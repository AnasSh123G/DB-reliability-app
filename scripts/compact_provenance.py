"""Read-only provenance checks for postprocessing the frozen training runs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PREFIX = "search-2026-09-26-robust-no-doy-mc20-rd56-"
TRIAL_REL = Path("data/ml/full_year/runs/compact-2026-09-26/trials/f1ac12b1742e3a0b")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def verify_training_key(key, root):
    """Refuse weights/results whose frozen input or implementation changed.

    Prepared binary identities use the original runner's size/mtime contract;
    the small encoding files, reference archive and source code use SHA-256.
    """
    root = Path(root)
    if file_hash(root / "scripts/train_compact_delay.py") != key["script_sha"]:
        raise ValueError("frozen training runner changed")
    for name, expected in key["production_code"].items():
        if file_hash(root / name) != expected:
            raise ValueError(f"frozen production code changed: {name}")
    trial = root / TRIAL_REL
    for name, field in (("inputs.json", "input_sha"), ("validation.npz", "reference_predictions_sha")):
        if file_hash(trial / name) != key[field]:
            raise ValueError(f"frozen reference changed: {name}")
    inputs = json.loads((trial / "inputs.json").read_text())
    for role, suffix in (("fit", "fit"), ("validation", "eval")):
        directory = root / "data/ml/full_year/prepared" / f"{PREFIX}{suffix}"
        if json.loads((directory / "key.json").read_text()) != key[role]:
            raise ValueError(f"frozen {role} prepared key changed")
        if json.loads((directory / "meta.json").read_text()) != key[f"{role}_metadata"]:
            raise ValueError(f"frozen {role} prepared metadata changed")
        if any(signature(inputs[name]) != key[role][name] for name in ("prep", "vocab")):
            raise ValueError(f"frozen {role} input encoding differs")
        for name, expected in key[f"{role}_files"].items():
            stat = (directory / f"{name}.bin").stat()
            if {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns} != expected:
                raise ValueError(f"frozen {role} prepared column changed: {name}")
    return inputs
