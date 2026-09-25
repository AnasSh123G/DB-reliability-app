"""The TabM delay model: its input preparation and ONNX inference.

The network is trained with PyTorch (scripts/experiment_nn.py, in the separate
ROCm venv) and exported to ONNX by scripts/export_tabm.py, so predicting needs
only onnxruntime -- the same file ONNX Runtime Web loads in a browser.

Inputs, exactly as in training:
- categoricals: the training-day vocabulary code + 1, so 0 = UNKNOWN
- numerics: standardised with training-day mean and std (NaN -> 0), a 0/1
  missing indicator where training had one, and sin/cos for times of day

`model_inputs` is the one implementation of this; the export script checks it
reproduces training's arrays bit for bit before it writes an artifact.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from dbrel.ml.features import CATEGORICAL, NUMERIC

KIND = "tabm"
MODEL_FILE = "model.onnx"
CURRENT_NAME, MANIFEST_NAME = "CURRENT", "manifest.json"  # the bake-off's names; this module must not
# import dbrel.ml.bakeoff, which needs the tree libraries the export venv does not have
CYCLIC = ("sched_min", "trip_start_min")
SIDES = ("dep", "arr")  # the model's output order
INPUT_CAT, INPUT_NUM, OUTPUT = "cat", "num", "quantiles"
BATCH = 4096  # rows per inference call; keeps activations small (rows x 16 members x 512)


def numeric_stats(features: dict[str, np.ndarray], train_mask: np.ndarray) -> dict[str, list[float]]:
    """Training-day mean and std of every numeric feature (std 0 -> 1)."""
    out = {}
    for name in NUMERIC:
        v = features[name].astype(np.float64)[train_mask]
        out[name] = [float(np.nanmean(v)), float(np.nanstd(v) or 1.0)]
    return out


def model_inputs(encoded: dict[str, np.ndarray], *, cards: list[int], numeric: list[str],
                 stats: dict[str, list[float]]) -> tuple[np.ndarray, np.ndarray]:
    """Encoded feature columns -> (categorical indices int64, numerics float32).

    `numeric` is the trained input column list: it says which missing
    indicators exist, and its length must come out the same here.
    """
    cats = np.column_stack([encoded[c].astype(np.int64) + 1 for c in CATEGORICAL])
    cats = np.minimum(cats, np.asarray(cards, dtype=np.int64) - 1)
    cols, names = [], []
    for name in NUMERIC:
        v = encoded[name].astype(np.float64)
        miss = np.isnan(v)
        mu, sd = stats[name]
        cols.append(np.where(miss, 0.0, (v - mu) / sd))
        names.append(name)
        if f"{name}_missing" in numeric:
            cols.append(miss.astype(np.float64))
            names.append(f"{name}_missing")
        if name in CYCLIC:
            angle = 2 * math.pi * np.nan_to_num(v) / 1440.0
            cols += [np.sin(angle), np.cos(angle)]
            names += [f"{name}_sin", f"{name}_cos"]
    if names != list(numeric):
        raise ValueError(f"numeric inputs {names} do not match the trained ones {list(numeric)}")
    return cats, np.column_stack(cols).astype(np.float32)


def save_artifact(models_dir: Path, manifest: dict, onnx_file: Path, stamp: str) -> Path:
    """Write a new artifact directory, then point CURRENT at it (the bake-off's layout).

    The directory is assembled under a hidden name and renamed when complete;
    earlier artifacts are never touched.
    """
    import json
    import shutil

    final = models_dir / stamp
    if final.exists():
        raise FileExistsError(f"{final} already exists")
    partial = models_dir / f".{stamp}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    shutil.copyfile(onnx_file, partial / MODEL_FILE)
    (partial / MANIFEST_NAME).write_text(json.dumps({**manifest, "kind": KIND, "model_file": MODEL_FILE},
                                                    indent=1))
    partial.rename(final)
    pointer = models_dir / (CURRENT_NAME + ".tmp")
    pointer.write_text(stamp + "\n")
    pointer.replace(models_dir / CURRENT_NAME)
    return final


class TabMPredictor:
    def __init__(self, artifact: Path, manifest: dict, session=None):
        self.manifest = manifest
        if session is None:
            import onnxruntime

            session = onnxruntime.InferenceSession(str(artifact / manifest["model_file"]),
                                                   providers=["CPUExecutionProvider"])
        self.session = session

    def predict(self, encoded: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """side -> (n, 3) minutes: p25, p50, p75 (ordered by construction)."""
        m = self.manifest
        cats, nums = model_inputs(encoded, cards=m["cards"], numeric=m["numeric"], stats=m["stats"])
        outs = [self.session.run([OUTPUT], {INPUT_CAT: cats[s:s + BATCH], INPUT_NUM: nums[s:s + BATCH]})[0]
                for s in range(0, len(cats), BATCH)]
        pred = np.concatenate(outs) if outs else np.zeros((0, len(SIDES), 3), dtype=np.float32)
        return {side: pred[:, i, :] for i, side in enumerate(SIDES)}
