"""Write the frozen, fresh-tested arrival model as a deployable artifact (task 5.1).

    dbrel ml launch arrival-artifact --memory 8G -- .venv/bin/python scripts/export_arrival_artifact.py

Copies the exact ONNX file the fresh holdout was scored with
(<experiment>/fresh-test/chosen/model.onnx), its input prep and the training
vocabulary into a new data/models/v2/<UTC>/ with an `arrival-net` manifest.
It does NOT move `CURRENT` -- `dbrel.ml.arrival.promote` does, after review.
Then reloads the artifact through `ArrivalNetPredictor` (the CLI/server path)
and checks it reproduces the saved fresh-test predictions on a sample.
Peak memory: the dataset (~2 GB) plus one encoded copy (~1 GB).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import probe_arrival as P  # noqa: E402
from dbrel.config import ML_MODELS_DIR  # noqa: E402
from dbrel.ml.arrival import ArrivalNetPredictor, save_arrival_artifact  # noqa: E402
from dbrel.ml.dataset import load_arrival_dataset  # noqa: E402
from dbrel.ml.features import FEATURES  # noqa: E402
from dbrel.ml.split import arrival_experiment_masks, restrict_to_training_vocab  # noqa: E402

EXPERIMENT = ROOT / "data" / "ml" / "arrival_runs" / "validation-search-20260926"
SAMPLE = 20_000
TOLERANCE = 1e-4  # minutes


def main() -> int:
    import onnxruntime

    final = json.loads((EXPERIMENT / "final.json").read_text())
    chosen_dir = EXPERIMENT / "fresh-test" / "chosen"
    prep_file = json.loads((chosen_dir / "prep.json").read_text())
    vocab = json.loads((EXPERIMENT / "training_vocab.json").read_text())
    current_before = (Path(ML_MODELS_DIR) / "CURRENT").read_text().strip()
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"experiment": str(EXPERIMENT.relative_to(ROOT)), "trial": final["chosen"]["trial"]},
        "config": prep_file["config"], "prep": prep_file["prep"], "output": prep_file["output"],
        "edges": prep_file["edges"], "point_rule": prep_file["config"]["point_rule"],
        "vocab": vocab, "features": list(FEATURES), "split": final["split"],
        "scores": {"validation": final["chosen"]["validation"], "fresh_test": final["overall"]["chosen"],
                   "incumbent_fresh_test": final["overall"]["incumbent"]},
        "disclosure": final["disclosure"],
        "versions": {"onnxruntime": onnxruntime.__version__, "numpy": np.__version__},
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = save_arrival_artifact(Path(ML_MODELS_DIR), manifest, {"model.onnx": chosen_dir / "model.onnx"},
                                     stamp)
    print(f"artifact written: {artifact}", flush=True)

    saved = json.loads((artifact / "manifest.json").read_text())
    ds = load_arrival_dataset(ROOT / "data" / "ml")
    _, masks = arrival_experiment_masks(ds, training=P.TRAINING, validation=P.VALIDATION,
                                        inspected=P.INSPECTED, final_test=True)
    enc, train_vocab = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    if json.loads(json.dumps(train_vocab)) != vocab:
        raise SystemExit("training vocabulary does not reproduce")
    with np.load(EXPERIMENT / "fresh-test" / "report" / "chosen.npz") as scored:
        stop_id, expected = scored["stop_id"], scored["predicted_min"]
    pick = np.random.default_rng(0).choice(len(stop_id), min(SAMPLE, len(stop_id)), replace=False)
    row_of = {int(s): i for i, s in enumerate(ds.stop_id)}
    rows = np.array([row_of[int(s)] for s in stop_id[pick]])
    got = ArrivalNetPredictor(artifact, saved).predict({k: v[rows] for k, v in enc.items()})
    diff = float(np.max(np.abs(got.point - expected[pick])))
    ok = diff <= TOLERANCE and (Path(ML_MODELS_DIR) / "CURRENT").read_text().strip() == current_before
    check = {"sample": len(rows), "max_abs_difference_min": diff, "tolerance_min": TOLERANCE,
             "current_unchanged": (Path(ML_MODELS_DIR) / "CURRENT").read_text().strip() == current_before,
             "asset_bytes": saved["asset_bytes"], "passed": ok}
    (EXPERIMENT / f"artifact-{stamp}.check.json").write_text(json.dumps(check, indent=1))
    print(json.dumps(check), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
