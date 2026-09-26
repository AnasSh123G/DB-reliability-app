"""Validation search over the approved arrival-only candidates (task 4.1).

Run as a capped job with the PyTorch-ROCm venv:

    dbrel ml launch arrival-search --memory 14G -- \\
        env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/search_arrival.py

The 12 trials Anas approved on 2026-09-26 (`trials()`): 4 histogram, 4 TabM,
4 residual MLP. Every trial fits on training history finalized before the
earliest validation cutoff (arrival minus 5 hours) and is scored on every
eligible validation arrival: within 1/3/5 minutes over all eligible arrivals,
coverage, MAE over scored arrivals, and date/segment/day-type breakdowns.
Ranking: validation within-3 (higher first), then MAE, then asset size.
No test or fresh-holdout date is loaded into any fit or score.

Trials are checkpointed in data/ml/arrival_runs/<experiment>/ (`arrival_runs`):
a killed run resumes with the finished trials kept. The deployed model's
arrival output is scored on the same validation rows for reference -- it was
early-stopped on these very dates, so its validation score is optimistic.
Nothing moves `CURRENT`.
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
sys.path.insert(0, str(ROOT / "scripts"))

import probe_arrival as P  # noqa: E402
from dbrel.config import ML_MODELS_DIR  # noqa: E402
from dbrel.ml.arrival import (  # noqa: E402
    ConditionalHistogram, arrival_inputs, fit_input_prep, incumbent_forecast,
)
from dbrel.ml.arrival_runs import ArrivalExperiment, experiment_key, run_trials  # noqa: E402
from dbrel.ml.dataset import load_arrival_dataset  # noqa: E402
from dbrel.ml.metrics import score_arrivals_with_breakdowns  # noqa: E402
from dbrel.ml.split import arrival_experiment_masks, restrict_to_training_vocab  # noqa: E402

MAX_TRIALS = 12
MAX_SECONDS = 3 * 3600  # 2 h GPU + 1 h CPU, the approved ceiling
CUTOFF_RULE = "training runs finalized at or before the earliest validation cutoff (arrival - 5 h)"


def trials() -> list[dict]:
    from dbrel.ml.arrival_nn import NetConfig

    def net(family, **kw):
        cfg = NetConfig(arch=family, **kw)
        return {"family": family, "scaling": "robust" if family == "mlp" else "standard",
                "config": cfg.as_dict()}

    return [
        *({"family": "histogram", "min_support": m, "point_rule": r}
          for m in (5, 20) for r in ("window", "median")),
        net("tabm", head="quantile", point_rule="median"),
        net("tabm", head="binned", point_rule="window"),
        net("tabm", head="binned", point_rule="window", hidden=128, tabm_k=4, train_emb_dim=8),
        net("tabm", head="binned", point_rule="window", hidden=512, tabm_k=16, train_emb_dim=32),
        net("mlp", head="quantile", point_rule="median"),
        net("mlp", head="binned", point_rule="window"),
        net("mlp", head="binned", point_rule="median"),
        net("mlp", head="binned", point_rule="window", hidden=512, blocks=3),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", default="validation-search-20260926")
    args = parser.parse_args()

    import torch

    from dbrel.ml.arrival_nn import ArrivalNet, NetConfig

    torch.set_num_threads(4)
    started = time.monotonic()
    ds = load_arrival_dataset(ROOT / "data" / "ml")
    split, masks = arrival_experiment_masks(ds, training=P.TRAINING, validation=P.VALIDATION,
                                            inspected=P.INSPECTED)
    enc, vocab = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    y = ds.arr_min.astype(np.float64)
    train_rows = np.flatnonzero(masks["train"])
    val_rows = np.flatnonzero(masks["validation"])
    dates = np.array(ds.meta["dates"])
    groups = {"date": dates[ds.day[val_rows]], "segment": ds.segment[val_rows],
              "holiday": ds.features["is_holiday"][val_rows]}
    print(f"{len(train_rows):,} training / {len(val_rows):,} validation arrivals "
          f"({time.monotonic() - started:.0f}s)", flush=True)

    space = trials()
    exp = ArrivalExperiment(ROOT / "data" / "ml" / "arrival_runs", args.experiment,
                            experiment_key(ds.meta, split.as_dict(), search_space={"trials": space},
                                           cutoff_rule=CUTOFF_RULE))
    exp.open()
    vocab_path = exp.dir / "training_vocab.json"
    if not vocab_path.exists():
        vocab_path.write_text(json.dumps(vocab))
    elif json.loads(vocab_path.read_text()) != json.loads(json.dumps(vocab)):
        raise SystemExit("the training vocabulary differs from the saved one; refusing to mix trials")
    preps: dict[str, tuple] = {}

    def prepared(scaling):
        if scaling not in preps:
            prep = fit_input_prep(enc, masks["train"], vocab, scaling)
            preps[scaling] = (prep, *arrival_inputs(enc, prep))
        return preps[scaling]

    def run(params, scratch: Path) -> dict:
        if params["family"] == "histogram":
            model = ConditionalHistogram(min_support=params["min_support"], point_rule=params["point_rule"])
            model.fit(enc, y, masks["train"])
            forecast = model.predict({k: v[val_rows] for k, v in enc.items()})
            model.export(scratch)
            extra = {"asset_bytes": sum((scratch / f).stat().st_size for f in ("histogram.json", "histogram.bin")),
                     "level_usage": {str(k): int(c) for k, c in
                                     zip(*np.unique(forecast.detail["level"], return_counts=True))}}
        else:
            prep, cats, nums = prepared(params["scaling"])
            net = ArrivalNet(NetConfig(**params["config"]), prep)
            history = net.fit(cats, nums, y, train_rows, val_rows, device="cuda",
                              log=lambda e: print(f"  {e}", flush=True))
            forecast = net.predict_arrays(cats[val_rows], nums[val_rows], device="cuda")
            torch.save(net.model.state_dict(), scratch / "model.pt")
            prep_text = json.dumps(prep)
            (scratch / "prep.json").write_text(prep_text)
            extra = {"history": history, "n_parameters": net.n_parameters(),
                     "asset_bytes": 4 * net.n_parameters() + len(prep_text.encode()),
                     "asset_bytes_note": "fp32 weights + prep; ONNX file measured at export"}
            del net
            torch.cuda.empty_cache()
        summary = {**score_arrivals_with_breakdowns(y[val_rows], forecast.point, groups), **extra,
                   "validation_dates": split.validation}
        o = summary["overall"]
        print(f"trial {params}: within-3 {o['within_3_min']:.4f}, MAE {o['mae_scored']:.3f}, "
              f"coverage {o['coverage']:.4f}", flush=True)
        return {"summary": summary, "stop_id": ds.stop_id[val_rows], "point": forecast.point,
                "missing_reason": forecast.missing_reason}

    done = run_trials(exp, space, run, max_trials=MAX_TRIALS, max_seconds=MAX_SECONDS)

    current = (Path(ML_MODELS_DIR) / "CURRENT").read_text().strip()
    incumbent = incumbent_forecast(ds, val_rows, Path(ML_MODELS_DIR) / current)
    inc_summary = score_arrivals_with_breakdowns(y[val_rows], incumbent.point, groups)
    ranking = sorted(
        ({"id": tid, "params": t["params"], "seconds": t["seconds"],
          **{k: t["summary"]["overall"][k] for k in ("within_1_min", "within_3_min", "within_5_min",
                                                     "mae_scored", "coverage", "eligible", "missing")},
          "asset_bytes": t["summary"]["asset_bytes"]} for tid, t in done.items()),
        key=lambda r: (-r["within_3_min"], r["mae_scored"], r["asset_bytes"]))
    report = {
        "experiment": args.experiment, "split": split.as_dict(), "cutoff_rule": CUTOFF_RULE,
        "timetable_asof_verified": ds.meta["timetable_asof_verified"],
        "trials_completed": len(done), "spent_seconds": exp.spent_seconds(),
        "ranking": ranking,
        "incumbent": {"artifact": current, "note": "early-stopped on these validation dates; optimistic",
                      "summary": inc_summary},
        "current_unchanged": (Path(ML_MODELS_DIR) / "CURRENT").read_text().strip() == current,
    }
    np.savez_compressed(exp.dir / "incumbent_validation.npz", stop_id=ds.stop_id[val_rows],
                        point=incumbent.point, pct=incumbent.percentiles)
    (exp.dir / "ranking.json").write_text(json.dumps(report, indent=1, default=float))
    print(json.dumps({k: report[k] for k in ("trials_completed", "spent_seconds")}), flush=True)
    for r in ranking:
        print(f"{r['within_3_min']:.4f}  MAE {r['mae_scored']:.3f}  {r['asset_bytes'] / 1e6:5.1f} MB  "
              f"{r['params'].get('family')} {r['params'].get('config', r['params'])}", flush=True)
    io = inc_summary["overall"]
    print(f"incumbent {current}: within-3 {io['within_3_min']:.4f}, MAE {io['mae_scored']:.3f}", flush=True)
    print(f"done in {(time.monotonic() - started) / 60:.1f} min -> {exp.dir / 'ranking.json'}", flush=True)
    return 0 if len(done) == len(space) else 1


if __name__ == "__main__":
    raise SystemExit(main())
