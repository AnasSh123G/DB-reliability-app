// Runs the delay model in the browser via ONNX Runtime Web. The server only
// encodes each call's features (see web/queries.py's _model_input) and never
// itself runs the model -- this module is where inference actually happens.
//
// Two artifact kinds, told apart by /api/model/<stamp>/meta.json:
// - "tabm" (v2): output "quantiles" (rows, [dep, arr], [p25, p50, p75]); the
//   point is the median.
// - "arrival-net", arrival only, one of two outputs:
//   - "probs" (rows, bins): the point follows `point_rule` -- the median, or the
//     centre of the most likely 2*half-minute window -- and p25/p75 come from
//     the same distribution (dbrel.ml.arrival.distribution_point, ported
//     exactly; scripts/browser_check verified it bit for bit).
//   - "quantiles" (rows, [p25, p50, p75]), already ordered by the network: the
//     point is the median.
import * as ort from "onnxruntime-web/wasm";
import type { ModelInput } from "@/lib/api";

const INPUT_CAT = "cat";
const INPUT_NUM = "num";
const TABM_SIDES = ["dep", "arr"] as const; // the v2 model's own output order (nn.py)
const QUANTILES = 3; // p25, p50, p75
const PCTS = [0.25, 0.5, 0.75];

export type Side = (typeof TABM_SIDES)[number];
export type PointRule = "median" | "window";

// One side's forecast in minutes. `point_min` is the median when rule is
// "median", the most likely ±window centre when rule is "window".
export type SidePrediction = {
  point_min: number;
  p25_min: number;
  p75_min: number;
  rule: PointRule;
};
// A side is absent when the model does not predict it (arrival-only models
// never predict departures) -- views then show no model figure for it.
export type ModelPrediction = Partial<Record<Side, SidePrediction>>;

type ModelMeta =
  | { kind: "tabm"; output: "quantiles"; sides: Side[]; point_rule: "median" }
  | {
      kind: "arrival-net";
      output: "probs";
      sides: ["arr"];
      point_rule: PointRule;
      edges: number[];
      window_half_min: number;
    }
  | {
      kind: "arrival-net";
      output: "quantiles";
      sides: ["arr"];
      point_rule: "median";
      edges: null;
      window_half_min: number;
    };

// One session and meta per model stamp, loaded at most once per stamp and
// reused for the rest of the visit -- a new stamp (a new CURRENT artifact)
// replaces them; an artifact's own bytes never change once written.
let loadedStamp: string | null = null;
let loaded: Promise<{ session: ort.InferenceSession; meta: ModelMeta }> | null = null;

function loadModel(stamp: string) {
  if (loaded && loadedStamp === stamp) return loaded;
  loadedStamp = stamp;
  loaded = Promise.all([
    ort.InferenceSession.create(`/api/model/${stamp}/model.onnx`, { executionProviders: ["wasm"] }),
    fetch(`/api/model/${stamp}/meta.json`).then(async (r) => {
      if (!r.ok) throw new Error(`model meta ${r.status}`);
      return (await r.json()) as ModelMeta;
    }),
  ]).then(([session, meta]) => ({ session, meta }));
  return loaded;
}

function distributionPoint(
  raw: Float32Array,
  row: number,
  edges: number[],
  rule: PointRule,
  half: number,
): SidePrediction {
  const nBins = edges.length - 1;
  const probs = new Float64Array(nBins);
  let total = 0;
  for (let b = 0; b < nBins; b++) total += raw[row * nBins + b]!;
  for (let b = 0; b < nBins; b++) probs[b] = raw[row * nBins + b]! / total;
  const cdf = new Float64Array(nBins);
  let acc = 0;
  for (let b = 0; b < nBins; b++) cdf[b] = acc += probs[b]!;
  const width = edges[1]! - edges[0]!;
  const pct = PCTS.map((q) => {
    let b = 0;
    for (let i = 0; i < nBins; i++) if (cdf[i]! < q) b++;
    b = Math.min(b, nBins - 1);
    const before = b > 0 ? cdf[b - 1]! : 0;
    const mass = probs[b]!;
    const frac = Math.min(Math.max(mass > 0 ? (q - before) / mass : 0, 0), 1);
    return edges[b]! + frac * width;
  });
  pct[1] = Math.max(pct[1]!, pct[0]!);
  pct[2] = Math.max(pct[2]!, pct[1]!);
  let point = pct[1]!;
  if (rule === "window") {
    const span = Math.round((2 * half) / width);
    let best = -Infinity;
    let start = 0;
    for (let i = 0; i + span <= nBins; i++) {
      const m = cdf[i + span - 1]! - (i > 0 ? cdf[i - 1]! : 0);
      if (m > best) {
        best = m;
        start = i;
      }
    }
    point = edges[start]! + half;
  }
  return { point_min: point, p25_min: pct[0]!, p75_min: pct[2]!, rule };
}

// Runs every given input through the model in one batch and returns one
// prediction per input, in the same order. `inputs` must be non-empty and
// every entry must have the same cat/num lengths (true of every model_input
// the API returns for one stamp).
export async function predictBatch(
  stamp: string,
  inputs: ModelInput[],
): Promise<ModelPrediction[]> {
  const [first] = inputs;
  if (!first) return [];
  const { session, meta } = await loadModel(stamp);
  const nCat = first.cat.length;
  const nNum = first.num.length;

  const catData = new BigInt64Array(inputs.length * nCat);
  const numData = new Float32Array(inputs.length * nNum);
  inputs.forEach((input, row) => {
    input.cat.forEach((value, col) => {
      catData[row * nCat + col] = BigInt(value);
    });
    input.num.forEach((value, col) => {
      numData[row * nNum + col] = value;
    });
  });

  const results = await session.run({
    [INPUT_CAT]: new ort.Tensor("int64", catData, [inputs.length, nCat]),
    [INPUT_NUM]: new ort.Tensor("float32", numData, [inputs.length, nNum]),
  });
  const out = results[meta.output]!.data as Float32Array;

  if (meta.kind === "arrival-net" && meta.output === "quantiles") {
    return inputs.map((_, row) => ({
      arr: {
        p25_min: out[row * QUANTILES]!,
        point_min: out[row * QUANTILES + 1]!,
        p75_min: out[row * QUANTILES + 2]!,
        rule: "median",
      },
    }));
  }
  if (meta.kind === "arrival-net") {
    return inputs.map((_, row) => ({
      arr: distributionPoint(out, row, meta.edges, meta.point_rule, meta.window_half_min),
    }));
  }
  return inputs.map((_, row) => {
    const prediction: ModelPrediction = {};
    TABM_SIDES.forEach((side, s) => {
      const base = (row * TABM_SIDES.length + s) * QUANTILES;
      prediction[side] = {
        p25_min: out[base]!,
        point_min: out[base + 1]!,
        p75_min: out[base + 2]!,
        rule: "median",
      };
    });
    return prediction;
  });
}
