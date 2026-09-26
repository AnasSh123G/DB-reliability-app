// Runs every exported arrival candidate in this browser and compares it with the
// Python reference (scripts/export_arrival.py). Served by scripts/browser_check.py;
// posts its findings to /result. Decision rules mirror dbrel.ml.arrival exactly.
import * as ort from "/ort/ort.wasm.min.mjs";

ort.env.wasm.wasmPaths = "/ort/";
ort.env.wasm.numThreads = 1; // no cross-origin isolation needed

const HALF = 3.0;
const PCTS = [0.25, 0.5, 0.75];

const fetchJson = async (p) => (await fetch(`/export/${p}`)).json();
const fetchBuf = async (p) => (await fetch(`/export/${p}`)).arrayBuffer();
const TYPES = { "<i8": BigInt64Array, "<f4": Float32Array, "<i4": Int32Array };
async function loadArray(spec) {
  return new TYPES[spec.dtype](await fetchBuf(`reference/${spec.file}`));
}

function distributionPoint(raw, row, nBins, edges, rule) {
  const probs = new Float64Array(nBins);
  let total = 0;
  for (let b = 0; b < nBins; b++) total += raw[row * nBins + b];
  for (let b = 0; b < nBins; b++) probs[b] = raw[row * nBins + b] / total;
  const cdf = new Float64Array(nBins);
  let acc = 0;
  for (let b = 0; b < nBins; b++) cdf[b] = acc += probs[b];
  const width = edges[1] - edges[0];
  const pct = PCTS.map((q) => {
    let b = 0;
    for (let i = 0; i < nBins; i++) if (cdf[i] < q) b++;
    b = Math.min(b, nBins - 1);
    const before = b > 0 ? cdf[b - 1] : 0;
    const mass = probs[b];
    const frac = Math.min(Math.max(mass > 0 ? (q - before) / mass : 0, 0), 1);
    return edges[b] + frac * width;
  });
  pct[1] = Math.max(pct[1], pct[0]);
  pct[2] = Math.max(pct[2], pct[1]);
  if (rule === "median") return { point: pct[1], pct };
  const span = Math.round((2 * HALF) / width);
  let best = -Infinity;
  let start = 0;
  for (let i = 0; i + span <= nBins; i++) {
    const m = cdf[i + span - 1] - (i > 0 ? cdf[i - 1] : 0);
    if (m > best) { best = m; start = i; }
  }
  return { point: edges[start] + HALF, pct };
}

function decide(raw, n, output, edges, rule) {
  const point = new Float64Array(n);
  const pct = new Float64Array(n * 3);
  if (output === "quantiles") {
    for (let r = 0; r < n; r++) {
      for (let j = 0; j < 3; j++) pct[r * 3 + j] = raw[r * 3 + j];
      point[r] = raw[r * 3 + 1];
    }
    return { point, pct };
  }
  const nBins = edges.length - 1;
  for (let r = 0; r < n; r++) {
    const d = distributionPoint(raw, r, nBins, edges, rule);
    point[r] = d.point;
    d.pct.forEach((v, j) => { pct[r * 3 + j] = v; });
  }
  return { point, pct };
}

function maxAbs(a, b) {
  let m = 0;
  for (let i = 0; i < a.length; i++) m = Math.max(m, Math.abs(a[i] - b[i]));
  return m;
}
function countDiff(a, b, tol) {
  let c = 0;
  for (let i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > tol) c++;
  return c;
}

async function checkNetwork(name, entry, ref) {
  const meta = await fetchJson(`${name}/prep.json`);
  const scaling = meta.prep.scaling;
  const cat = await loadArray(ref.inputs[`cat_${scaling}`]);
  const num = await loadArray(ref.inputs[`num_${scaling}`]);
  const [n, nCat] = ref.inputs[`cat_${scaling}`].shape;
  const nNum = ref.inputs[`num_${scaling}`].shape[1];
  const expected = {};
  for (const [k, spec] of Object.entries(ref.expected[name])) expected[k] = await loadArray(spec);

  const t0 = performance.now();
  const session = await ort.InferenceSession.create(`/export/${name}/model.onnx`, { executionProviders: ["wasm"] });
  const loadMs = performance.now() - t0;
  const feeds = { cat: new ort.Tensor("int64", cat, [n, nCat]), num: new ort.Tensor("float32", num, [n, nNum]) };
  const t1 = performance.now();
  const raw = (await session.run(feeds))[meta.output].data;
  const batchMs = performance.now() - t1;

  const single = [];
  for (let i = 0; i < 21; i++) {
    const s = performance.now();
    await session.run({ cat: new ort.Tensor("int64", cat.slice(0, nCat), [1, nCat]),
                        num: new ort.Tensor("float32", num.slice(0, nNum), [1, nNum]) });
    single.push(performance.now() - s);
  }
  single.sort((a, b) => a - b);

  const browser = decide(raw, n, meta.output, meta.edges, meta.config.point_rule);
  const logic = decide(expected.raw, n, meta.output, meta.edges, meta.config.point_rule);
  return {
    output: meta.output, rows: n,
    raw_max_abs_vs_python: maxAbs(raw, expected.raw),
    js_rule_on_python_raw_max_abs: maxAbs(logic.point, expected.point),
    js_rule_on_python_raw_pct_max_abs: maxAbs(logic.pct, expected.pct),
    browser_point_max_abs: maxAbs(browser.point, expected.point),
    browser_points_differing: countDiff(browser.point, expected.point, 1e-3),
    load_ms: loadMs, batch_ms: batchMs, single_call_median_ms: single[10],
  };
}

async function checkHistogram(ref) {
  const meta = await fetchJson("histogram/histogram.json");
  const blob = await fetchBuf("histogram/histogram.bin");
  const inputs = await loadArray(ref.inputs.histogram);
  const [n, nCol] = ref.inputs.histogram.shape;
  const expPoint = await loadArray(ref.expected.histogram.point);
  const expPct = await loadArray(ref.expected.histogram.pct);
  const expLevel = await loadArray(ref.expected.histogram.level);
  const colIndex = Object.fromEntries(meta.input_columns.map((c, i) => [c, i]));
  const tables = meta.levels.map((lv) => ({
    ...lv,
    keys: new BigInt64Array(blob, lv.offset, lv.count),
    n: new Int32Array(blob, lv.offset + 8 * lv.count, lv.count),
    window: new Float32Array(blob, lv.offset + 12 * lv.count, lv.count),
    pct: new Float32Array(blob, lv.offset + 16 * lv.count, 3 * lv.count),
  }));
  const last = tables.length - 1;
  const t0 = performance.now();
  const point = new Float64Array(n).fill(NaN);
  const pct = new Float64Array(n * 3).fill(NaN);
  const level = new Int32Array(n).fill(-1);
  for (let r = 0; r < n; r++) {
    for (let li = 0; li < tables.length; li++) {
      const t = tables[li];
      let key = 0n;
      let valid = true;
      t.columns.forEach((c, j) => {
        const comp = inputs[r * nCol + colIndex[c]];
        const radix = BigInt(t.radix[j]);
        if (comp <= 0n || comp >= radix) valid = false;
        key = key * radix + (valid ? comp : 0n);
      });
      if (!valid) continue;
      let lo = 0, hi = t.count;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (t.keys[mid] < key) lo = mid + 1; else hi = mid; }
      if (lo >= t.count || t.keys[lo] !== key || t.n[lo] < (li === last ? 1 : meta.min_support)) continue;
      level[r] = li;
      for (let j = 0; j < 3; j++) pct[r * 3 + j] = t.pct[lo * 3 + j];
      point[r] = meta.point_rule === "window" ? t.window[lo] : t.pct[lo * 3 + 1];
      break;
    }
  }
  const lookupMs = performance.now() - t0;
  let levelDiff = 0;
  for (let r = 0; r < n; r++) if (level[r] !== expLevel[r]) levelDiff++;
  return {
    rows: n, point_max_abs: maxAbs(point, expPoint), pct_max_abs: maxAbs(pct, expPct),
    levels_differing: levelDiff, missing: level.filter((v) => v < 0).length,
    asset_fetch_bytes: blob.byteLength, lookup_ms_all_rows: lookupMs,
  };
}

async function main() {
  const manifest = await fetchJson("manifest.json");
  const ref = await fetchJson("reference/reference.json");
  const result = { user_agent: navigator.userAgent, ort_version: ort.env.versions?.web ?? null, candidates: {} };
  for (const [name, entry] of Object.entries(manifest.candidates)) {
    try {
      result.candidates[name] = entry.kind === "onnx" ? await checkNetwork(name, entry, ref) : await checkHistogram(ref);
    } catch (e) {
      result.candidates[name] = { error: String(e && e.stack ? e.stack : e) };
    }
  }
  return result;
}

main()
  .catch((e) => ({ error: String(e && e.stack ? e.stack : e) }))
  .then(async (result) => {
    document.getElementById("out").textContent = JSON.stringify(result, null, 1);
    await fetch("/result", { method: "POST", body: JSON.stringify(result) });
  });
