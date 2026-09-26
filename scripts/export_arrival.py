"""Prototype browser exports of the arrival-only candidates, with Python-side parity.

Run as a capped job with the PyTorch-ROCm venv:

    dbrel ml launch arrival-export --memory 14G -- \\
        env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/export_arrival.py

Small fitted assets, not trained candidates: each network gets one epoch on
the probe's date-stratified training subset (full training vocabulary, so the
embedding tables -- and the file size -- are realistic); the histogram is
fitted on the full training history. For each candidate it writes:

- networks: `model.onnx` (outputs `quantiles` (n, 3) or `probs` (n, bins),
  members averaged) and `prep.json` (input layout and statistics)
- histogram: `histogram.json` + `histogram.bin`

and checks ONNX Runtime (Python) against PyTorch, and the reloaded histogram
export against the fitted one. `reference/` holds 1,000 validation rows'
inputs and the Python outputs `scripts/browser_check.py` compares the browser
against. Prediction-specific assets are counted against 35,000,000 bytes; the
shared ONNX Runtime WASM runtime is reported separately. Output goes to a new
data/ml/arrival_exports/<UTC>/; `CURRENT` is checked to be unchanged.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import probe_arrival as P  # noqa: E402
from dbrel.config import ML_MODELS_DIR  # noqa: E402
from dbrel.ml.arrival import (  # noqa: E402
    HIST_COLUMNS, ConditionalHistogram, arrival_inputs, bin_edges, distribution_point, fit_input_prep,
    histogram_inputs,
)
from dbrel.ml.dataset import load_arrival_dataset  # noqa: E402
from dbrel.ml.split import arrival_experiment_masks, restrict_to_training_vocab  # noqa: E402

BUDGET_BYTES = 35_000_000
REFERENCE_ROWS = 1000
ONNX_TOLERANCE = {"quantiles": 1e-4, "probs": 1e-5}  # minutes / probability, ONNX vs PyTorch
RUNTIME_WASM = ROOT / "bahn-buddy-predict/node_modules/onnxruntime-web/dist/ort-wasm-simd-threaded.wasm"
RUNTIME_JS = ROOT / "bahn-buddy-predict/node_modules/onnxruntime-web/dist/ort.wasm.min.mjs"


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%SZ}] {message}", flush=True)


def _save(path: Path, array: np.ndarray, dtype: str) -> dict:
    data = np.ascontiguousarray(array, dtype=dtype)
    path.write_bytes(data.tobytes())
    return {"file": path.name, "dtype": dtype, "shape": list(data.shape)}


def _current() -> str | None:
    pointer = Path(ML_MODELS_DIR) / "CURRENT"
    return pointer.read_text() if pointer.exists() else None


def export_network(torch, onnx, onnxruntime, net, cats, nums, ref_rows, out: Path) -> dict:
    output = "probs" if net.cfg.head == "binned" else "quantiles"

    class Exported(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, cat, num):
            return net._raw(cat, num)

    model = net.model.cpu().eval()
    c = torch.as_tensor(cats[ref_rows])
    n = torch.as_tensor(nums[ref_rows])
    with torch.no_grad():
        reference = net._raw(c, n).numpy()
    out.mkdir(parents=True)
    torch.onnx.export(Exported(model).eval(), (c[:64], n[:64]), out / "model.onnx",
                      input_names=["cat", "num"], output_names=[output],
                      dynamic_axes={"cat": {0: "rows"}, "num": {0: "rows"}, output: {0: "rows"}},
                      dynamo=False)
    onnx.checker.check_model(str(out / "model.onnx"))
    session = onnxruntime.InferenceSession(str(out / "model.onnx"), providers=["CPUExecutionProvider"])
    got = session.run([output], {"cat": cats[ref_rows], "num": nums[ref_rows]})[0]
    diff = float(np.abs(got - reference).max())
    (out / "prep.json").write_text(json.dumps({
        "prep": net.prep, "config": net.cfg.as_dict(), "output": output,
        "edges": net.edges.tolist() if output == "probs" else None,
    }, indent=1))
    files = ["model.onnx", "prep.json"]
    raw = got.astype(np.float64)
    if output == "probs":
        point, pct = distribution_point(raw, net.edges, net.cfg.point_rule)
    else:
        pct, point = raw, raw[:, 1]
    torch_forecast = net.predict_arrays(cats[ref_rows], nums[ref_rows])
    return {
        "kind": "onnx", "files": files, "output": output, "point_rule": net.cfg.point_rule,
        "scaling": net.prep["scaling"], "n_parameters": net.n_parameters(),
        "onnx_vs_torch_max_abs": diff, "onnx_tolerance": ONNX_TOLERANCE[output],
        "onnx_passed": diff <= ONNX_TOLERANCE[output],
        "points_differing_torch_vs_onnx": int(np.count_nonzero(
            np.abs(torch_forecast.point - point.astype(np.float32)) > 1e-4)),
        "_expected": {"raw": got.astype(np.float32), "point": point.astype(np.float32),
                      "pct": pct.astype(np.float32)},
    }


def main() -> int:
    import onnx
    import onnxruntime
    import torch

    from dbrel.ml.arrival_nn import ArrivalNet, NetConfig

    current_before = _current()
    torch.set_num_threads(4)
    rng = np.random.default_rng(0)
    ds = load_arrival_dataset(ROOT / "data" / "ml")
    split, masks = arrival_experiment_masks(ds, training=P.TRAINING, validation=P.VALIDATION,
                                            inspected=P.INSPECTED)
    enc, vocab = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    y = ds.arr_min.astype(np.float64)
    train_all = np.flatnonzero(masks["train"])
    sub_train = P._stratified(train_all, ds.day, P.SUBSET_PER_DATE, rng)
    val_all = np.flatnonzero(masks["validation"])
    sub_val = np.sort(rng.choice(val_all, P.VAL_SUBSET, replace=False))
    ref_rows = np.sort(rng.choice(val_all, REFERENCE_ROWS, replace=False))
    log(f"loaded {len(y):,} arrivals; reference rows {len(ref_rows)}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "data" / "ml" / "arrival_exports" / stamp
    ref_dir = out / "reference"
    ref_dir.mkdir(parents=True)
    reference = {"rows": len(ref_rows), "stop_id": ds.stop_id[ref_rows].tolist(), "inputs": {}, "expected": {}}
    candidates = {}

    preps = {}
    for scaling in ("standard", "robust"):
        prep = fit_input_prep(enc, masks["train"], vocab, scaling)
        cats, nums = arrival_inputs(enc, prep)
        preps[scaling] = (prep, cats, nums)
        reference["inputs"][f"cat_{scaling}"] = _save(ref_dir / f"cat_{scaling}.i64", cats[ref_rows], "<i8")
        reference["inputs"][f"num_{scaling}"] = _save(ref_dir / f"num_{scaling}.f32", nums[ref_rows], "<f4")

    for name, cfg in P._configs(NetConfig).items():
        prep, cats, nums = preps["robust" if cfg.arch == "mlp" else "standard"]
        net = ArrivalNet(replace(cfg, epochs=1, patience=1), prep, bin_edges())
        started = time.monotonic()
        net.fit(cats, nums, y, sub_train, sub_val, device="cuda")
        entry = export_network(torch, onnx, onnxruntime, net, cats, nums, ref_rows, out / name)
        entry["fit_seconds"] = round(time.monotonic() - started, 1)
        expected = entry.pop("_expected")
        reference["expected"][name] = {k: _save(ref_dir / f"{name}.{k}.f32", v, "<f4") for k, v in expected.items()}
        candidates[name] = entry
        log(f"{name}: {json.dumps(entry)}")
        del net
        torch.cuda.empty_cache()

    hist = ConditionalHistogram(min_support=P.MIN_SUPPORT, point_rule="window").fit(enc, y, masks["train"])
    files = hist.export(out / "histogram")
    loaded = ConditionalHistogram.load_export(out / "histogram")
    ref_enc = {k: v[ref_rows] for k, v in enc.items()}
    a, b = hist.predict(ref_enc), loaded.predict(ref_enc)
    names = [lv.name for lv in hist.levels]
    reference["inputs"]["histogram"] = _save(ref_dir / "histogram.i64", histogram_inputs(ref_enc), "<i8")
    reference["expected"]["histogram"] = {
        "point": _save(ref_dir / "histogram.point.f32", a.point, "<f4"),
        "pct": _save(ref_dir / "histogram.pct.f32", a.percentiles, "<f4"),
        "level": _save(ref_dir / "histogram.level.i32",
                       np.array([names.index(v) if v else -1 for v in a.detail["level"]]), "<i4"),
    }
    candidates["histogram"] = {
        "kind": "histogram", "files": files, "point_rule": hist.point_rule, "input_columns": list(HIST_COLUMNS),
        "export_round_trip_passed": bool(
            np.array_equal(a.point, b.point, equal_nan=True)
            and np.array_equal(a.percentiles, b.percentiles, equal_nan=True)),
    }
    log(f"histogram: {json.dumps(candidates['histogram'])}")

    for name, entry in candidates.items():
        directory = out / name
        entry["asset_bytes"] = sum((directory / f).stat().st_size for f in entry["files"])
        entry["eligible_by_size"] = entry["asset_bytes"] <= BUDGET_BYTES
    (ref_dir / "reference.json").write_text(json.dumps(reference))
    vocab_bytes = len(json.dumps(vocab).encode())
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": "3.4 browser-runtime prototype; small fitted assets, not trained candidates",
        "dataset": {"built_at": ds.meta["built_at"], "n_rows": ds.meta["n_rows"]},
        "split": split.as_dict(), "budget_bytes": BUDGET_BYTES, "candidates": candidates,
        "shared_runtime_bytes": {RUNTIME_WASM.name: RUNTIME_WASM.stat().st_size,
                                 RUNTIME_JS.name: RUNTIME_JS.stat().st_size},
        "server_side_vocab_json_bytes": vocab_bytes,
        "versions": {"torch": torch.__version__, "onnx": onnx.__version__,
                     "onnxruntime": onnxruntime.__version__, "numpy": np.__version__},
    }
    manifest["current_unchanged"] = _current() == current_before
    manifest["python_checks_passed"] = bool(manifest["current_unchanged"] and all(
        e.get("onnx_passed", e.get("export_round_trip_passed")) for e in candidates.values()))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    log(f"{'PASSED' if manifest['python_checks_passed'] else 'FAILED'} -> {out}")
    return 0 if manifest["python_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
