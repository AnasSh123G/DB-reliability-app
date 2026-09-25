"""Is the GPU XGBoost build trustworthy on this card? (design.md D10, step 4)

Fits the same quantile model with the same parameters on the same ~1M
training-day rows twice -- `device="cuda"` (the HIP device on the ROCm build)
and `device="cpu"` -- and compares mean validation pinball loss. They must
agree within `TOLERANCE`, with no errors on the categorical columns, before the
bake-off may run XGBoost on the GPU. Both timings are recorded in
`data/ml/gpu_check.json`, which the bake-off copies into its manifest so the
report shows any hardware advantage.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from dbrel.ml.candidates import XGBoostCandidate, mean_pinball, xgboost_has_gpu
from dbrel.ml.dataset import Dataset
from dbrel.ml.features import feature_matrix
from dbrel.ml.split import masks_for, restrict_to_training_vocab, split_dates

RESULT_NAME = "gpu_check.json"
TOLERANCE = 0.01
SAMPLE_ROWS = 1_000_000
VALIDATION_ROWS = 300_000
PARAMS = {"max_depth": 8, "eta": 0.1, "min_child_weight": 20.0, "max_cat_to_onehot": 4, "max_bin": 256}
ROUNDS = 300


def run_gpu_check(ds: Dataset, out_dir: Path, *, threads: int, log=print) -> dict:
    ok, note = xgboost_has_gpu()
    result: dict = {"checked_at": time.time(), "gpu_available": ok, "note": note}
    if not ok:
        result.update(passed=False, reason=f"no GPU: {note}")
        _write(out_dir, result)
        return result

    split = split_dates(ds.meta["rows_per_date"])
    masks = masks_for(ds.day, ds.meta["dates"], split)
    features, _ = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    X = feature_matrix(features)
    y = ds.dep_min
    rng = np.random.default_rng(0)
    train_rows = np.flatnonzero(masks["train"] & ~np.isnan(y))
    val_rows = np.flatnonzero(masks["validation"] & ~np.isnan(y))
    train_rows = np.sort(rng.choice(train_rows, size=min(SAMPLE_ROWS, len(train_rows)), replace=False))
    val_rows = np.sort(rng.choice(val_rows, size=min(VALIDATION_ROWS, len(val_rows)), replace=False))

    runs = {}
    for device in ("cuda", "cpu"):
        candidate = XGBoostCandidate(PARAMS, threads=threads, max_rounds=ROUNDS, device=device)
        try:
            candidate.fit(X[train_rows], y[train_rows], X[val_rows], y[val_rows])
            pred, crossed = candidate.predict(X[val_rows])
        except Exception as exc:  # noqa: BLE001 - any failure fails the check
            result.update(passed=False, reason=f"{device} fit failed: {type(exc).__name__}: {exc}"[:500])
            _write(out_dir, result)
            return result
        runs[device] = {
            "mean_pinball": mean_pinball(y[val_rows], pred),
            "seconds": round(candidate.fit_seconds, 1),
            "rounds": candidate.best_rounds,
            "crossed_share": crossed,
        }
        log(f"{device}: mean pinball {runs[device]['mean_pinball']:.4f} in {candidate.fit_seconds:.1f}s")

    gpu, cpu = runs["cuda"]["mean_pinball"], runs["cpu"]["mean_pinball"]
    relative = abs(gpu - cpu) / cpu
    passed = relative <= TOLERANCE
    result.update(
        passed=passed,
        relative_difference=relative,
        speedup=runs["cpu"]["seconds"] / max(runs["cuda"]["seconds"], 1e-9),
        runs=runs,
        rows={"train": int(len(train_rows)), "validation": int(len(val_rows))},
        params=PARAMS,
        threads=threads,
        reason=None if passed else f"GPU and CPU pinball differ by {relative:.2%} (> {TOLERANCE:.0%})",
    )
    _write(out_dir, result)
    return result


def _write(out_dir: Path, result: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / RESULT_NAME).write_text(json.dumps(result, indent=1))


def load_gpu_check(out_dir: Path) -> dict | None:
    path = out_dir / RESULT_NAME
    return json.loads(path.read_text()) if path.exists() else None
