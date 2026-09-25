"""Per-library checkpoints: a bake-off killed at any point resumes from its last finished step.

Layout, one directory per library under `data/ml/runs/`:

    key.json            what the saved work is valid for (dataset, split, search space ...)
    optuna.sqlite3      the tuning study, committed trial by trial
    tuning.json         the finished tuning result
    <side>.<model files> the side's final model, in the library's own format
    <side>.result.npz   its test predictions
    <side>.fit.json     its fit description and scores -- written last: the side's "done" mark
    done                the library is complete

JSON and npz files are written to a temporary name and renamed, so a kill
mid-write never leaves a file that looks finished. Model files are written in
place, but only count once `<side>.fit.json` exists, so a half-written model
is simply refitted.

Saved work is reused only when `key.json` matches exactly: a different dataset,
split, feature list or search space is refused by name, never mixed in.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path

import numpy as np

from dbrel.config import ML_MODEL_VERSION
from dbrel.ml.features import FEATURES
from dbrel.ml.metrics import QUANTILES

KEY_NAME = "key.json"
TUNING_NAME = "tuning.json"
DONE_NAME = "done"
STUDY_NAME = "optuna.sqlite3"


class CheckpointMismatch(RuntimeError):
    pass


def search_space_hash(library: str) -> str:
    from dbrel.ml.tune import SEARCH_SPACES

    return hashlib.sha256(inspect.getsource(SEARCH_SPACES[library]).encode()).hexdigest()[:16]


def checkpoint_key(meta: dict, split: dict, library: str, *, sample_rows: int, max_rounds: int) -> dict:
    return {
        "model_version": ML_MODEL_VERSION,
        "library": library,
        "dataset": {"built_at": meta["built_at"], "db_fingerprint": meta["db_fingerprint"],
                    "n_rows": meta["n_rows"]},
        "split": split,
        "features": list(FEATURES),
        "quantiles": list(QUANTILES),
        "search_space": search_space_hash(library),
        "sample_rows": sample_rows,
        "max_rounds": max_rounds,
    }


def key_differences(saved: dict, now: dict, prefix: str = "") -> list[str]:
    out = []
    for name in sorted(set(saved) | set(now)):
        a, b = saved.get(name), now.get(name)
        if isinstance(a, dict) and isinstance(b, dict) and name != "split":
            out += key_differences(a, b, f"{prefix}{name}.")
        elif a != b:
            if name == "split":
                a = {k: f"{v[0]}..{v[-1]}" for k, v in (a or {}).items()}
                b = {k: f"{v[0]}..{v[-1]}" for k, v in (b or {}).items()}
            out.append(f"{prefix}{name}: saved {a}, now {b}")
    return out


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def _write_json(path: Path, value) -> None:
    _write_atomic(path, json.dumps(value, indent=1, default=_json_default).encode())


def _json_default(value):
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"not JSON serialisable: {type(value)}")


class LibraryCheckpoint:
    def __init__(self, root: Path, library: str, key: dict):
        self.library = library
        self.dir = root / library
        self.key = key

    # -- lifecycle ------------------------------------------------------------
    def exists(self) -> bool:
        return (self.dir / KEY_NAME).exists()

    def open(self) -> None:
        """Create the directory, or check that the saved work is for this exact setup."""
        self.dir.mkdir(parents=True, exist_ok=True)
        for leftover in self.dir.glob("*.tmp"):
            leftover.unlink()
        path = self.dir / KEY_NAME
        if path.exists():
            saved = json.loads(path.read_text())
            diff = key_differences(saved, json.loads(json.dumps(self.key, default=_json_default)))
            if diff:
                raise CheckpointMismatch(
                    f"saved {self.library} work in {self.dir} is for a different setup -- "
                    + "; ".join(diff)
                    + ". Move that directory aside to start this library afresh."
                )
        else:
            _write_json(path, self.key)

    @property
    def study_url(self) -> str:
        return f"sqlite:///{self.dir / STUDY_NAME}"

    def is_done(self) -> bool:
        return (self.dir / DONE_NAME).exists()

    def mark_done(self) -> None:
        _write_atomic(self.dir / DONE_NAME, b"")

    # -- tuning ---------------------------------------------------------------
    def has_tuning(self) -> bool:
        return (self.dir / TUNING_NAME).exists()

    def save_tuning(self, tuning: dict, *, device: str, threads: int) -> None:
        _write_json(self.dir / TUNING_NAME, {"tuning": tuning, "device": device, "threads": threads})

    def load_tuning(self) -> dict:
        return json.loads((self.dir / TUNING_NAME).read_text())

    # -- sides ----------------------------------------------------------------
    def prefix(self, side: str) -> Path:
        return self.dir / side

    def has_side(self, side: str) -> bool:
        return (self.dir / f"{side}.fit.json").exists() and (self.dir / f"{side}.result.npz").exists()

    def save_side(self, side: str, candidate, *, pred_test: np.ndarray, validation_mean_pinball: float,
                  crossed_share_validation: float, crossed_share_test: float, threads: int) -> None:
        files = candidate.save(self.prefix(side))
        import io

        buffer = io.BytesIO()
        np.savez(buffer, pred_test=pred_test)
        _write_atomic(self.dir / f"{side}.result.npz", buffer.getvalue())
        _write_json(self.dir / f"{side}.fit.json", {
            "fit": candidate.describe(),
            "files": files,
            "threads": threads,
            "validation_mean_pinball": validation_mean_pinball,
            "crossed_share_validation": crossed_share_validation,
            "crossed_share_test": crossed_share_test,
        })

    def load_side(self, side: str) -> dict:
        info = json.loads((self.dir / f"{side}.fit.json").read_text())
        with np.load(self.dir / f"{side}.result.npz") as data:
            info["_pred_test"] = data["pred_test"]
        return info
