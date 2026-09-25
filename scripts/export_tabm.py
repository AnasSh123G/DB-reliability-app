"""Export the trained TabM network to ONNX and make it the model predict-delay uses.

Run with the SEPARATE venv that holds PyTorch-ROCm, as a capped job:

    .venv/bin/dbrel ml launch export-tabm --memory 10G -- \\
        data/build/torch-rocm/venv/bin/python scripts/export_tabm.py

Reads data/ml/experiments/nn_tabm.json (settings, input columns, scores) and
nn_tabm_model.pt (weights), plus the dataset for the training-day statistics
the weights were trained against. Before anything is written it checks:

1. dbrel.ml.nn.model_inputs builds exactly the arrays training built
2. the loaded weights reproduce the saved test MAE of both sides
3. the ONNX file computes what PyTorch computes, on a sample of test rows

Then it writes a new artifact under data/models/v<N>/ and moves CURRENT to it;
earlier artifacts stay as they are.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import experiment_nn as X  # noqa: E402
from dbrel.config import ML_MODEL_VERSION, ML_MODELS_DIR  # noqa: E402
from dbrel.ml.dataset import load_dataset  # noqa: E402
from dbrel.ml.features import FEATURES  # noqa: E402
from dbrel.ml.memory import format_memory  # noqa: E402
from dbrel.ml.metrics import QUANTILES, score  # noqa: E402
from dbrel.ml.nn import INPUT_CAT, INPUT_NUM, OUTPUT, SIDES, model_inputs, numeric_stats, save_artifact  # noqa: E402
from dbrel.ml.split import masks_for, restrict_to_training_vocab, split_dates  # noqa: E402

MAE_TOLERANCE = 1e-3     # minutes: reloaded weights vs the saved test score
ONNX_TOLERANCE = 1e-3    # minutes: ONNX vs PyTorch, per prediction


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%SZ}] {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="nn_tabm", help="experiment result to export")
    parser.add_argument("--sample", type=int, default=20_000, help="test rows compared ONNX vs PyTorch")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    import onnx
    import onnxruntime
    import torch

    torch.set_num_threads(args.threads)
    exp = ROOT / "data" / "ml" / "experiments"
    result = json.loads((exp / f"{args.name}.json").read_text())
    X.HP.update(result["hp"])

    ds = load_dataset(ROOT / "data" / "ml")
    split = split_dates(ds.meta["rows_per_date"])
    if split.as_dict() != result["split"]:
        raise SystemExit("the dataset's date split differs from the one the model was trained on")
    masks = masks_for(ds.day, ds.meta["dates"], split)
    features, vocab = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    log(f"dataset loaded, {len(ds.day):,} rows; {format_memory()}")

    # 1. the prediction-side input code builds what training built
    cats, cards, nums, names, y = X.build_arrays(ds, masks, features)
    if cards != result["cards"] or names != result["numeric"]:
        raise SystemExit(f"inputs differ from training: cards {cards} vs {result['cards']}, "
                         f"columns {names} vs {result['numeric']}")
    stats = numeric_stats(features, masks["train"])
    c2, n2 = model_inputs(features, cards=cards, numeric=names, stats=stats)
    if not (np.array_equal(cats, c2) and np.array_equal(nums, n2)):
        raise SystemExit("dbrel.ml.nn.model_inputs does not reproduce training's arrays")
    del c2, n2
    log("check 1 passed: prediction inputs equal training inputs on every row")

    # 2. the weights reproduce the saved test scores
    model = X.build_model(torch, cards, nums.shape[1])
    model.load_state_dict(torch.load(exp / f"{args.name}_model.pt", map_location="cpu"))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device).eval()
    test_rows = np.flatnonzero(masks["test"])
    pred = X.predict(torch, model, cats, nums, test_rows, device)
    reproduced = {}
    for s, side in enumerate(SIDES):
        has = ~np.isnan(y[test_rows, s])
        got = score(y[test_rows[has], s], pred[has, s])["mae_p50"]
        want = result["sides"][side]["test"]["overall"]["mae_p50"]
        reproduced[side] = {"saved": want, "reloaded": got}
        if abs(got - want) > MAE_TOLERANCE:
            raise SystemExit(f"{side}: reloaded weights give test MAE {got:.4f}, the saved result says {want:.4f}")
    log(f"check 2 passed: test MAE reproduced {reproduced} ({device})")

    # 3. ONNX computes what PyTorch computes
    model = model.cpu().eval()
    rng = np.random.default_rng(0)
    sample = np.sort(rng.choice(test_rows, min(args.sample, len(test_rows)), replace=False))
    with torch.no_grad():
        reference = X.members_mean(model(torch.as_tensor(cats[sample]), torch.as_tensor(nums[sample]))).numpy()

    class Exported(torch.nn.Module):
        """(cat, num) -> (n, side, quantile), the ensemble members already averaged."""

        def __init__(self, net):
            super().__init__()
            self.net = net

        def forward(self, cat, num):
            return X.members_mean(self.net(cat, num))

    with tempfile.TemporaryDirectory(dir=exp) as tmp:
        onnx_file = Path(tmp) / "model.onnx"
        example = (torch.as_tensor(cats[sample[:64]]), torch.as_tensor(nums[sample[:64]]))
        torch.onnx.export(Exported(model).eval(), example, onnx_file,
                          input_names=[INPUT_CAT, INPUT_NUM], output_names=[OUTPUT],
                          dynamic_axes={INPUT_CAT: {0: "rows"}, INPUT_NUM: {0: "rows"}, OUTPUT: {0: "rows"}},
                          dynamo=False)
        onnx.checker.check_model(str(onnx_file))
        session = onnxruntime.InferenceSession(str(onnx_file), providers=["CPUExecutionProvider"])
        got = np.concatenate([session.run([OUTPUT], {INPUT_CAT: cats[sample[s:s + 4096]],
                                                     INPUT_NUM: nums[sample[s:s + 4096]]})[0]
                              for s in range(0, len(sample), 4096)])
        max_diff = float(np.abs(got - reference).max())
        if max_diff > ONNX_TOLERANCE:
            raise SystemExit(f"ONNX differs from PyTorch by up to {max_diff:.5f} min")
        size = onnx_file.stat().st_size
        log(f"check 3 passed: ONNX vs PyTorch max difference {max_diff:.2e} min on {len(sample):,} test rows; "
            f"file {size / 1e6:.1f} MB")

        manifest = {
            "model_version": ML_MODEL_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": {"result": f"data/ml/experiments/{args.name}.json",
                       "weights": f"data/ml/experiments/{args.name}_model.pt"},
            "dataset": {"built_at": ds.meta["built_at"], "db_fingerprint": ds.meta["db_fingerprint"],
                        "n_rows": ds.meta["n_rows"]},
            "split": split.as_dict(),
            "features": list(FEATURES),
            "quantiles": list(QUANTILES),
            "sides": list(SIDES),
            "hp": result["hp"],
            "vocab": vocab,
            "cards": cards,
            "numeric": names,
            "stats": stats,
            "scores": {side: {"validation_mean_pinball": result["sides"][side]["validation_mean_pinball"],
                              "test": result["sides"][side]["test"]["overall"]} for side in SIDES},
            "checks": {"test_mae": reproduced, "onnx_max_difference": max_diff, "onnx_sample_rows": len(sample)},
            "size_bytes": size,
            "versions": {"torch": torch.__version__, "onnx": onnx.__version__,
                         "onnxruntime": onnxruntime.__version__, "numpy": np.__version__},
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifact = save_artifact(ML_MODELS_DIR, manifest, onnx_file, stamp)
    log(f"artifact: {artifact} (CURRENT now points at it)")
    return 0


if __name__ == "__main__":
    started = time.monotonic()
    code = main()
    log(f"done in {time.monotonic() - started:.0f}s")
    raise SystemExit(code)
