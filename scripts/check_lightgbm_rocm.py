"""Does AMD's ROCm LightGBM build compute correct results on this GPU?

Run with the SEPARATE venv from scripts/build_lightgbm_rocm.sh, never the
project's .venv:

    data/build/lightgbm-rocm/venv/bin/python scripts/check_lightgbm_rocm.py [--threads 2]

Fits the same LightGBM model with the same parameters on the same rows of the
real dataset (data/ml/, with its high-cardinality categorical columns) twice,
on device_type="cuda" (the HIP device on the ROCm build) and on "cpu", for a
squared-error model and a median (quantile 0.5) model. Squared error is
included because it is the test that exposed the XGBoost ROCm build: its
quantile objective re-fits leaf values and so only partly shows a broken GPU.
The GPU passes if both losses are within 1% of the CPU's.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbrel.ml.dataset import load_dataset  # noqa: E402
from dbrel.ml.features import CATEGORICAL, FEATURES, feature_matrix  # noqa: E402
from dbrel.ml.split import masks_for, restrict_to_training_vocab, split_dates  # noqa: E402

TOLERANCE = 0.01


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--rounds", type=int, default=300)
    args = parser.parse_args()

    import lightgbm as lgb

    ds = load_dataset(ROOT / "data" / "ml")
    split = split_dates(ds.meta["rows_per_date"])
    masks = masks_for(ds.day, ds.meta["dates"], split)
    features, _ = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    X = feature_matrix(features)
    y = ds.dep_min
    rng = np.random.default_rng(0)
    tr = np.flatnonzero(masks["train"] & ~np.isnan(y))
    va = np.flatnonzero(masks["validation"] & ~np.isnan(y))
    tr = np.sort(rng.choice(tr, size=min(args.rows, len(tr)), replace=False))
    va = np.sort(rng.choice(va, size=min(300_000, len(va)), replace=False))

    from importlib.metadata import version

    results = {"lightgbm": version("amd_lightgbm"), "rows": len(tr), "threads": args.threads, "runs": {}}
    for objective in ("regression", "quantile"):
        for device in ("cuda", "cpu"):
            params = {
                "objective": objective, "alpha": 0.5, "device_type": device,
                "num_leaves": 255, "learning_rate": 0.1, "min_data_in_leaf": 100,
                "num_threads": args.threads, "verbosity": -1, "seed": 0, "deterministic": True,
            }
            train = lgb.Dataset(X[tr], label=y[tr], categorical_feature=list(range(len(CATEGORICAL))),
                                feature_name=list(FEATURES))
            started = time.monotonic()
            try:
                booster = lgb.train(params, train, num_boost_round=args.rounds)
            except Exception as exc:  # noqa: BLE001 - report any failure
                results["runs"][f"{objective}/{device}"] = {"error": f"{type(exc).__name__}: {exc}"[:400]}
                print(f"{objective:10s} {device:4s} FAILED: {exc}", flush=True)
                continue
            seconds = time.monotonic() - started
            pred = booster.predict(X[va], num_threads=args.threads)
            if objective == "regression":
                loss = float(np.sqrt(np.mean((y[va] - pred) ** 2)))
            else:
                diff = y[va] - pred
                loss = float(np.mean(np.maximum(0.5 * diff, -0.5 * diff)))
            results["runs"][f"{objective}/{device}"] = {"loss": loss, "seconds": round(seconds, 1)}
            print(f"{objective:10s} {device:4s} loss={loss:.4f} {seconds:7.1f}s", flush=True)

    verdicts = {}
    for objective in ("regression", "quantile"):
        gpu = results["runs"].get(f"{objective}/cuda", {})
        cpu = results["runs"].get(f"{objective}/cpu", {})
        if "loss" in gpu and "loss" in cpu:
            rel = abs(gpu["loss"] - cpu["loss"]) / cpu["loss"]
            verdicts[objective] = {"relative_difference": rel, "speedup": cpu["seconds"] / gpu["seconds"],
                                   "passed": rel <= TOLERANCE}
        else:
            verdicts[objective] = {"passed": False, "reason": "a fit failed"}
    results["verdicts"] = verdicts
    results["passed"] = all(v["passed"] for v in verdicts.values())
    out = ROOT / "data" / "ml" / "lightgbm_rocm_check.json"
    out.write_text(json.dumps(results, indent=1))
    print(json.dumps(verdicts, indent=1))
    print("PASSED" if results["passed"] else "FAILED", "->", out)
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
