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

    Prepared binary identities include size, mtime and SHA-256; encoding files,
    reference predictions and model/feature source code also use SHA-256.
    """
    root = Path(root)
    if file_hash(root / "scripts/train_recency_delay.py") != key["script_sha"]:
        raise ValueError("frozen training runner changed")
    if file_hash(root / 'scripts/recency_weights.py') != key['recency_code_sha']:
        raise ValueError('recency weight/loss implementation changed')
    for name, expected in key["production_code"].items():
        if file_hash(root / name) != expected:
            raise ValueError(f"frozen production code changed: {name}")
    # The key names its input trial; predictions of TRIAL_REL, whose inputs were
    # prepared, are the reference for every condition.
    trial = root / key.get("trial", TRIAL_REL)
    for path, field in ((trial / "inputs.json", "input_sha"),
                        (root / TRIAL_REL / "validation.npz", "reference_predictions_sha")):
        if file_hash(path) != key[field]:
            raise ValueError(f"frozen reference changed: {path.name}")
    inputs = json.loads((trial / "inputs.json").read_text())
    prepared = {"prep": json.loads((root / TRIAL_REL / "inputs.json").read_text())["prep"], "vocab": inputs["vocab"]}
    for role, suffix in (("fit", "fit"), ("validation", "eval")):
        directory = root / key["prepared_root"] / f"{PREFIX}{suffix}"
        if root.resolve() not in directory.resolve().parents:
            raise ValueError("corrected data must stay inside repository")
        if json.loads((directory / "key.json").read_text()) != key[role]:
            raise ValueError(f"frozen {role} prepared key changed")
        if json.loads((directory / "meta.json").read_text()) != key[f"{role}_metadata"]:
            raise ValueError(f"frozen {role} prepared metadata changed")
        if any(signature(prepared[name]) != key[role][name] for name in ("prep", "vocab")):
            raise ValueError(f"frozen {role} input encoding differs")
        for name, expected in key[f"{role}_files"].items():
            stat = (directory / f"{name}.bin").stat()
            if {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": file_hash(directory / f"{name}.bin")} != expected:
                raise ValueError(f"frozen {role} prepared column changed: {name}")
    return inputs
