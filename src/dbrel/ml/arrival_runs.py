"""Resumable arrival-only experiments: every finished trial is saved, none is mixed across setups.

Layout, one directory per experiment under `data/ml/arrival_runs/`:

    key.json                what the saved trials are valid for
    trials/<id>/trial.json  a finished trial: params, validation summary, seconds, files
    trials/<id>/validation.npz  its per-arrival validation points (stop_id, point, reason)
    trials/<id>/...         model files the trial chose to keep

`<id>` is a hash of the trial's full params (candidate family, architecture,
head, decision rule, loss and hyperparameters), so two different settings can
never share a directory. A trial is assembled under `trials/.<id>.partial` and
renamed when complete; a kill leaves only a partial directory, which the next
`open()` removes and the trial reruns.

`key.json` names the dataset, date split, forecast-cutoff policy, feature list,
search space and selection objective. A resume against anything else is
refused with the differences named. Nothing here writes outside the experiment
directory, and an experiment directory inside the deployed models directory is
refused, so `CURRENT` can never move.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from dbrel.config import ML_MODELS_DIR
from dbrel.ml.checkpoint import CheckpointMismatch, _json_default, _write_atomic, _write_json, key_differences
from dbrel.ml.features import FEATURES

KEY_NAME = "key.json"
TRIALS = "trials"
TRIAL_NAME = "trial.json"
VALIDATION_NAME = "validation.npz"
OBJECTIVE = {"primary": "validation within_3_min (inclusive, all eligible arrivals)",
             "tie_breakers": ["mae_scored", "asset_bytes"]}
MAE_OBJECTIVE = {"primary": "validation mae_scored of the median estimate, full coverage first",
                 "tie_breakers": ["coverage", "asset_bytes"]}


def trial_rank_key(summary: dict, asset_bytes: int) -> tuple:
    """Sort key, best first: full coverage, then lowest MAE, then coverage, then smaller files.

    A trial that leaves arrivals unpredicted never outranks a full-coverage one
    on MAE alone -- its MAE is over an easier subset.
    """
    coverage = summary.get("coverage") or 0.0
    mae = summary.get("mae_scored")
    return (coverage < 1.0, mae if mae is not None else float("inf"), -coverage, asset_bytes)


def rank_trials(trials: dict[str, dict]) -> list[str]:
    """Trial ids from `ArrivalExperiment.completed()`, best first by `trial_rank_key`."""
    return sorted(trials, key=lambda tid: trial_rank_key(trials[tid]["summary"]["validation"],
                                                         trials[tid]["summary"].get("asset_bytes", 0)))


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, default=_json_default, separators=(",", ":"))


def trial_id(params: dict) -> str:
    return hashlib.sha256(_canonical(params).encode()).hexdigest()[:16]


def experiment_key(meta: dict, split: dict, *, search_space: dict, cutoff_rule: str) -> dict:
    if meta.get("kind") != "arrival-only":
        raise ValueError("arrival experiments need the arrival-only dataset")
    return {
        "dataset": {"kind": meta["kind"], "built_at": meta["built_at"],
                    "db_fingerprint": meta["db_fingerprint"], "n_rows": meta["n_rows"]},
        "split": split,
        "cutoff": {"hours": meta["cutoff_hours"], "rule": cutoff_rule,
                   "timetable_asof_verified": meta["timetable_asof_verified"]},
        "features": list(FEATURES),
        "search_space": json.loads(_canonical(search_space)),
        "objective": OBJECTIVE,
    }


class ArrivalExperiment:
    def __init__(self, root: Path, name: str, key: dict):
        self.dir = Path(root) / name
        resolved, models = self.dir.resolve(), Path(ML_MODELS_DIR).resolve()
        if resolved == models or models in resolved.parents:
            raise ValueError(f"experiments must not be written inside {models}")
        self.key = json.loads(_canonical(key))

    @property
    def trials_dir(self) -> Path:
        return self.dir / TRIALS

    def open(self) -> None:
        """Create the experiment, or check the saved one is for this exact setup."""
        self.trials_dir.mkdir(parents=True, exist_ok=True)
        for leftover in [*self.trials_dir.glob(".*.partial"), *self.trials_dir.glob(".*.scratch")]:
            shutil.rmtree(leftover)
        path = self.dir / KEY_NAME
        if path.exists():
            diff = key_differences(json.loads(path.read_text()), self.key)
            if diff:
                raise CheckpointMismatch(
                    f"saved arrival experiment in {self.dir} is for a different setup -- "
                    + "; ".join(diff) + ". Use a new experiment name or move that directory aside.")
        else:
            _write_json(path, self.key)

    def completed(self) -> dict[str, dict]:
        out = {}
        for path in sorted(self.trials_dir.glob(f"*/{TRIAL_NAME}")):
            if not path.parent.name.startswith("."):
                out[path.parent.name] = json.loads(path.read_text())
        return out

    def has(self, params: dict) -> bool:
        return (self.trials_dir / trial_id(params) / TRIAL_NAME).exists()

    def load_validation(self, params: dict) -> dict[str, np.ndarray]:
        with np.load(self.trials_dir / trial_id(params) / VALIDATION_NAME) as data:
            return {k: data[k] for k in data.files}

    def save_trial(self, params: dict, *, summary: dict, seconds: float, stop_id: np.ndarray,
                   point: np.ndarray, missing_reason: np.ndarray,
                   write_files: Callable[[Path], list[str]] | None = None) -> str:
        tid = trial_id(params)
        final = self.trials_dir / tid
        if final.exists():
            raise FileExistsError(f"trial {tid} is already saved")
        partial = self.trials_dir / f".{tid}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir()
        files = write_files(partial) if write_files else []
        buffer = io.BytesIO()
        np.savez(buffer, stop_id=np.asarray(stop_id, dtype=np.int64),
                 point=np.asarray(point, dtype=np.float32),
                 missing_reason=np.asarray(missing_reason, dtype=str))
        _write_atomic(partial / VALIDATION_NAME, buffer.getvalue())
        _write_json(partial / TRIAL_NAME, {"id": tid, "params": params, "summary": summary,
                                           "seconds": round(float(seconds), 1), "files": files,
                                           "saved_at": time.time()})
        partial.rename(final)
        return tid

    def spent_seconds(self) -> float:
        return sum(t["seconds"] for t in self.completed().values())


def run_trials(exp: ArrivalExperiment, trials: list[dict], run: Callable[[dict, Path], dict], *,
               max_trials: int, max_seconds: float, log=print) -> dict[str, dict]:
    """Run each not-yet-saved trial in order, within what is left of both budgets.

    `run(params, scratch_dir)` returns {"summary", "stop_id", "point",
    "missing_reason"} and may leave model files in `scratch_dir` to keep.
    Trials already saved count against the budgets and are never rerun.
    """
    exp.open()
    for params in trials:
        done = exp.completed()
        if exp.has(params):
            continue
        spent = exp.spent_seconds()
        if len(done) >= max_trials or spent >= max_seconds:
            log(f"budget reached: {len(done)} trials, {spent / 60:.1f} min")
            break
        scratch = exp.trials_dir / f".{trial_id(params)}.scratch"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir()
        started = time.monotonic()
        try:
            out = run(params, scratch)
            seconds = time.monotonic() - started

            def keep(target: Path) -> list[str]:
                names = []
                for item in sorted(scratch.iterdir()):
                    item.rename(target / item.name)
                    names.append(item.name)
                return names

            tid = exp.save_trial(params, summary=out["summary"], seconds=seconds, stop_id=out["stop_id"],
                                 point=out["point"], missing_reason=out["missing_reason"], write_files=keep)
            log(f"trial {tid} saved ({seconds:.0f}s)")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return exp.completed()
