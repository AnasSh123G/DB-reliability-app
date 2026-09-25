"""Experiment: LightGBM on the AMD GPU with every feature numeric.

Run with the SEPARATE venv from scripts/build_lightgbm_rocm.sh:

    data/build/lightgbm-rocm/venv/bin/python scripts/experiment_lightgbm_gpu_numeric.py

The ROCm LightGBM build fails on this card as soon as any feature is
categorical (design.md D10), so this variant has none. Each categorical
feature becomes two numeric columns: its code, and how often that value occurs
on the training days (frequency encoding -- no target information, so nothing
leaks). Everything else -- rows, split, quantiles, sorted outputs, metrics --
is the bake-off's, so the test scores line up with its report.

Step 1 checks the GPU against the CPU on the same 1M rows (squared error and
median; must agree within 1%). Only if that passes does step 2 fit the three
quantile models on all training days on the GPU and score them. Results go to
data/ml/experiments/lightgbm_gpu_numeric.json. CPU work is capped at
`--threads` (default 2) so a bake-off running alongside keeps its threads.
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
from dbrel.ml.features import CATEGORICAL, FEATURES, UNKNOWN  # noqa: E402
from dbrel.ml.metrics import QUANTILES, order_quantiles, score_with_breakdowns  # noqa: E402
from dbrel.ml.split import masks_for, restrict_to_training_vocab, split_dates  # noqa: E402

TOLERANCE = 0.01
PARAMS = {
    "num_leaves": 255,
    "learning_rate": 0.05,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "max_bin": 255,
    "verbosity": -1,
    "seed": 0,
}


def numeric_matrix(features: dict[str, np.ndarray], train_mask: np.ndarray) -> tuple[np.ndarray, list[str]]:
    columns, names = [], []
    for name in CATEGORICAL:
        codes = features[name].astype(np.int64)
        counts = np.bincount(codes[train_mask & (codes != UNKNOWN)], minlength=int(codes.max()) + 2)
        freq = np.where(codes == UNKNOWN, 0, counts[np.maximum(codes, 0)])
        columns += [np.where(codes == UNKNOWN, np.nan, codes).astype(np.float32), freq.astype(np.float32)]
        names += [f"{name}_code", f"{name}_freq"]
    for name in FEATURES:
        if name not in CATEGORICAL:
            columns.append(features[name].astype(np.float32))
            names.append(name)
    return np.column_stack(columns), names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--check-rows", type=int, default=1_000_000)
    parser.add_argument("--max-rounds", type=int, default=5000)
    args = parser.parse_args()

    import lightgbm as lgb

    started = time.monotonic()
    ds = load_dataset(ROOT / "data" / "ml")
    split = split_dates(ds.meta["rows_per_date"])
    masks = masks_for(ds.day, ds.meta["dates"], split)
    features, _ = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    X, names = numeric_matrix(features, masks["train"])
    out = {"split": split.as_dict(), "params": PARAMS, "threads": args.threads, "features": names}
    print(f"matrix {X.shape}, {time.monotonic() - started:.0f}s", flush=True)

    # Step 1: GPU vs CPU on the same rows.
    y = ds.dep_min
    rng = np.random.default_rng(0)
    tr = np.sort(rng.choice(np.flatnonzero(masks["train"] & ~np.isnan(y)), args.check_rows, replace=False))
    va = np.sort(rng.choice(np.flatnonzero(masks["validation"] & ~np.isnan(y)), 300_000, replace=False))
    check = {}
    for objective in ("regression", "quantile"):
        losses = {}
        for device in ("cuda", "cpu"):
            p = {**PARAMS, "objective": objective, "alpha": 0.5, "device_type": device,
                 "num_threads": args.threads}
            t = time.monotonic()
            booster = lgb.train(p, lgb.Dataset(X[tr], label=y[tr], feature_name=names), num_boost_round=200)
            seconds = time.monotonic() - t
            pred = booster.predict(X[va], num_threads=args.threads)
            diff = y[va] - pred
            loss = float(np.sqrt(np.mean(diff ** 2))) if objective == "regression" else \
                float(np.mean(np.maximum(0.5 * diff, -0.5 * diff)))
            losses[device] = {"loss": loss, "seconds": round(seconds, 1)}
            print(f"check {objective:10s} {device:4s} loss={loss:.4f} {seconds:6.1f}s", flush=True)
        rel = abs(losses["cuda"]["loss"] - losses["cpu"]["loss"]) / losses["cpu"]["loss"]
        check[objective] = {**losses, "relative_difference": rel, "passed": rel <= TOLERANCE,
                            "speedup": losses["cpu"]["seconds"] / losses["cuda"]["seconds"]}
    out["check"] = check
    out["check_passed"] = all(c["passed"] for c in check.values())
    exp_dir = ROOT / "data" / "ml" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    path = exp_dir / "lightgbm_gpu_numeric.json"
    if not out["check_passed"]:
        path.write_text(json.dumps(out, indent=1, default=float))
        print("GPU check FAILED -- not training", flush=True)
        return 1

    # Step 2: full quantile fits on the GPU, scored like the bake-off.
    out["sides"] = {}
    for side in ("dep", "arr"):
        y = ds.dep_min if side == "dep" else ds.arr_min
        rows = {k: np.flatnonzero(masks[k] & ~np.isnan(y)) for k in ("train", "validation", "test")}
        train = lgb.Dataset(X[rows["train"]], label=y[rows["train"]], feature_name=names, free_raw_data=False)
        valid = lgb.Dataset(X[rows["validation"]], label=y[rows["validation"]], reference=train)
        preds_val, preds_test, fits = [], [], []
        for alpha in QUANTILES:
            t = time.monotonic()
            booster = lgb.train(
                {**PARAMS, "objective": "quantile", "metric": "quantile", "alpha": alpha,
                 "device_type": "cuda", "num_threads": args.threads},
                train, num_boost_round=args.max_rounds, valid_sets=[valid],
                callbacks=[lgb.early_stopping(50, verbose=False)],
            )
            seconds = time.monotonic() - t
            fits.append({"alpha": alpha, "rounds": booster.best_iteration, "seconds": round(seconds, 1)})
            print(f"{side} q{int(alpha * 100)}: {booster.best_iteration} rounds in {seconds / 60:.1f} min", flush=True)
            preds_val.append(booster.predict(X[rows["validation"]], num_iteration=booster.best_iteration))
            preds_test.append(booster.predict(X[rows["test"]], num_iteration=booster.best_iteration))
        pv, _ = order_quantiles(np.column_stack(preds_val))
        pt, crossed = order_quantiles(np.column_stack(preds_test))
        yv, yt = y[rows["validation"]], y[rows["test"]]
        groups = {"segment": ds.segment[rows["test"]],
                  "holiday": features["is_holiday"][rows["test"]].astype(int),
                  "date": np.array(ds.meta["dates"], dtype=object)[ds.day[rows["test"]]]}
        val_pinball = float(np.mean([np.mean(np.maximum(a * (yv - pv[:, i]), (a - 1) * (yv - pv[:, i])))
                                     for i, a in enumerate(QUANTILES)]))
        out["sides"][side] = {"fits": fits, "validation_mean_pinball": val_pinball,
                              "crossed_share_test": crossed,
                              "test": score_with_breakdowns(yt, pt, groups)}
        o = out["sides"][side]["test"]["overall"]
        print(f"{side}: test MAE p50 {o['mae_p50']:.3f}, band coverage {o['band_coverage']:.1%}, "
              f"val mean pinball {val_pinball:.4f}", flush=True)
    out["seconds"] = round(time.monotonic() - started, 1)
    path.write_text(json.dumps(out, indent=1, default=float))
    print(f"done in {out['seconds'] / 60:.0f} min -> {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
