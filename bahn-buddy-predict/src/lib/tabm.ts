// Runs the delay model in the browser via ONNX Runtime Web. The server only
// encodes each call's features (see web/queries.py's _model_input) and never
// itself runs the model -- this module is where inference actually happens.
import * as ort from "onnxruntime-web/wasm";
import type { ModelInput } from "@/lib/api";

const INPUT_CAT = "cat";
const INPUT_NUM = "num";
const OUTPUT = "quantiles";
const SIDES = ["dep", "arr"] as const; // the model's own output order (nn.py)
const QUANTILES = 3; // p25, p50, p75

export type Side = (typeof SIDES)[number];

// p25, p50, p75 minutes for one side, or null if that side has nothing to
// show (mirrors DelayPred, without lo/hi collapsing to one number).
export type SidePrediction = { p25_min: number; p50_min: number; p75_min: number };
export type ModelPrediction = Record<Side, SidePrediction>;

// One session per model stamp, loaded at most once per stamp and reused for
// the rest of the visit -- a new stamp (a new CURRENT artifact) replaces it,
// matching the fact that an artifact's own bytes never change once written.
let sessionStamp: string | null = null;
let session: Promise<ort.InferenceSession> | null = null;

function loadSession(stamp: string): Promise<ort.InferenceSession> {
  if (session && sessionStamp === stamp) return session;
  sessionStamp = stamp;
  session = ort.InferenceSession.create(`/api/model/${stamp}/model.onnx`, {
    executionProviders: ["wasm"],
  });
  return session;
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
  const ortSession = await loadSession(stamp);
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

  const results = await ortSession.run({
    [INPUT_CAT]: new ort.Tensor("int64", catData, [inputs.length, nCat]),
    [INPUT_NUM]: new ort.Tensor("float32", numData, [inputs.length, nNum]),
  });
  const out = results[OUTPUT]!.data as Float32Array;

  return inputs.map((_, row) => {
    const prediction = {} as ModelPrediction;
    SIDES.forEach((side, s) => {
      const base = (row * SIDES.length + s) * QUANTILES;
      prediction[side] = {
        p25_min: out[base]!,
        p50_min: out[base + 1]!,
        p75_min: out[base + 2]!,
      };
    });
    return prediction;
  });
}
