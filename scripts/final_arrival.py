"""Locked evaluation on the sealed fresh holdout (task 4.2) -- runs once.

    dbrel ml launch arrival-final --memory 14G -- \\
        env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/final_arrival.py

Takes the rank-1 trial of the validation search as frozen (validation within-3,
then MAE, then size -- the approved rule, applied before any test outcome was
read), exports it to ONNX, and scores that exported file -- not the PyTorch
weights -- plus the deployed model's arrival output on every eligible arrival
of the first three complete, uninspected dates after 2026-09-22. Training
history, vocabulary and input statistics are the search's own (checked to
reproduce exactly). Writes <experiment>/fresh-test/ (per-arrival rows and
report.json via `save_arrival_report`) and final.json. Refuses to run if a
fresh-test result already exists. `CURRENT` is checked to be unchanged.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import probe_arrival as P  # noqa: E402
from export_arrival import export_network  # noqa: E402
from dbrel.config import DB_PATH, ML_MODELS_DIR  # noqa: E402
from dbrel.derive.stop_averages import MAX_PLAUSIBLE_DELAY_S  # noqa: E402
from dbrel.ml.arrival import (  # noqa: E402
    ArrivalForecast, arrival_inputs, distribution_point, fit_input_prep, incumbent_forecast,
)
from dbrel.ml.arrival_runs import trial_id  # noqa: E402
from dbrel.ml.dataset import load_arrival_dataset  # noqa: E402
from dbrel.ml.report import save_arrival_report  # noqa: E402
from dbrel.ml.split import arrival_experiment_masks, restrict_to_training_vocab  # noqa: E402

EXPERIMENT = ROOT / "data" / "ml" / "arrival_runs" / "validation-search-20260926"
BATCH = 65536


def exclusions(dates: list[str]) -> dict[str, int]:
    """Why scheduled arrivals on the test dates are not eligible (counts only)."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    marks = ",".join("?" * len(dates))
    row = conn.execute(f"""
        SELECT COUNT(*),
               SUM(ts.cancelled = 1 OR ts.arr_cancelled = 1),
               SUM(ts.cancelled = 0 AND ts.arr_cancelled = 0 AND ts.quality != 'RECORDED'),
               SUM(ts.cancelled = 0 AND ts.arr_cancelled = 0 AND ts.quality = 'RECORDED'
                   AND ts.arr_delay_s IS NULL),
               SUM(ts.cancelled = 0 AND ts.arr_cancelled = 0 AND ts.quality = 'RECORDED'
                   AND ABS(ts.arr_delay_s) > {MAX_PLAUSIBLE_DELAY_S}),
               SUM(ts.is_holiday IS NULL)
        FROM trip_stop ts JOIN trip t ON t.id = ts.trip_id
        WHERE t.service_date IN ({marks}) AND ts.supposed_arrival_utc IS NOT NULL
    """, dates).fetchone()
    by_quality = dict(conn.execute(f"""
        SELECT ts.quality, COUNT(*) FROM trip_stop ts JOIN trip t ON t.id = ts.trip_id
        WHERE t.service_date IN ({marks}) AND ts.supposed_arrival_utc IS NOT NULL
          AND ts.cancelled = 0 AND ts.arr_cancelled = 0 AND ts.quality != 'RECORDED'
        GROUP BY 1
    """, dates).fetchall())
    conn.close()
    names = ("scheduled_arrivals", "cancelled", "not_recorded", "recorded_without_arrival_delay",
             "implausible_over_2h", "holiday_flag_missing")
    return {**{k: int(v or 0) for k, v in zip(names, row)},
            **{f"not_recorded_{q}": int(n) for q, n in by_quality.items()}}


def main() -> int:
    import onnx
    import onnxruntime
    import torch

    from dbrel.ml.arrival_nn import ArrivalNet, NetConfig

    out = EXPERIMENT / "fresh-test"
    if (out / "report").exists() or (EXPERIMENT / "final.json").exists():
        raise SystemExit("the fresh holdout has already been scored once; refusing to score it again")
    if out.exists():  # an interrupted export: no outcome was read yet
        import shutil

        shutil.rmtree(out)
    ranking = json.loads((EXPERIMENT / "ranking.json").read_text())
    chosen = ranking["ranking"][0]
    params = chosen["params"]
    trial_dir = EXPERIMENT / "trials" / trial_id(params)
    current_before = (Path(ML_MODELS_DIR) / "CURRENT").read_text().strip()

    ds = load_arrival_dataset(ROOT / "data" / "ml")
    split, masks = arrival_experiment_masks(ds, training=P.TRAINING, validation=P.VALIDATION,
                                            inspected=P.INSPECTED, final_test=True)
    if split.as_dict()["validation"] != ranking["split"]["validation"]:
        raise SystemExit("validation dates differ from the search")
    enc, vocab = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    if json.loads(json.dumps(vocab)) != json.loads((EXPERIMENT / "training_vocab.json").read_text()):
        raise SystemExit("training vocabulary does not reproduce the search's")
    y = ds.arr_min.astype(np.float64)
    test_rows = np.flatnonzero(masks["test"])
    print(f"fresh test dates {split.test}: {len(test_rows):,} eligible arrivals", flush=True)

    if params["family"] == "histogram":
        raise SystemExit("a histogram winner would be scored from its export; not needed for this ranking")
    prep = json.loads((trial_dir / "prep.json").read_text())
    if json.loads(json.dumps(fit_input_prep(enc, masks["train"], vocab, params["scaling"]))) != prep:
        raise SystemExit("input statistics do not reproduce the search's")
    cats, nums = arrival_inputs(enc, prep)
    net = ArrivalNet(NetConfig(**params["config"]), prep)
    net.model.load_state_dict(torch.load(trial_dir / "model.pt", map_location="cpu"))
    ref_rows = test_rows[np.linspace(0, len(test_rows) - 1, 1000).astype(int)]
    export = export_network(torch, onnx, onnxruntime, net, cats, nums, ref_rows, out / "chosen")
    export.pop("_expected")
    if not export["onnx_passed"]:
        raise SystemExit(f"exported model differs from PyTorch: {export['onnx_vs_torch_max_abs']}")

    session = onnxruntime.InferenceSession(str(out / "chosen" / "model.onnx"),
                                           providers=["CPUExecutionProvider"])
    point = np.full(len(test_rows), np.nan)
    pct = np.full((len(test_rows), 3), np.nan)
    for s in range(0, len(test_rows), BATCH):
        r = test_rows[s:s + BATCH]
        raw = session.run([export["output"]], {"cat": cats[r], "num": nums[r]})[0].astype(np.float64)
        if export["output"] == "probs":
            point[s:s + len(r)], pct[s:s + len(r)] = distribution_point(raw, net.edges, net.cfg.point_rule)
        else:
            pct[s:s + len(r)], point[s:s + len(r)] = raw, raw[:, 1]
    chosen_forecast = ArrivalForecast.build(point, np.full(len(test_rows), ""), net.cfg.point_rule, pct)

    incumbent = incumbent_forecast(ds, test_rows, Path(ML_MODELS_DIR) / current_before)
    report = save_arrival_report(
        out / "report", ds=ds, rows=test_rows,
        predictions={"chosen": chosen_forecast.point, "incumbent": incumbent.point},
        phase="fresh-test", exclusions=exclusions(split.test),
    )
    final = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chosen": {"trial": trial_id(params), "params": params, "validation": {
            k: chosen[k] for k in ("within_1_min", "within_3_min", "within_5_min", "mae_scored")},
            "point_rule": net.cfg.point_rule, "export": export,
            "onnx_bytes": (out / "chosen" / "model.onnx").stat().st_size,
            "asset_bytes": sum((out / "chosen" / f).stat().st_size for f in export["files"])},
        "incumbent": {"artifact": current_before,
                      "note": "trained through 2026-09-12 with early stopping on 2026-09-13..16"},
        "split": split.as_dict(), "fresh_test_dates": split.test,
        "training_history": "finalized before the earliest validation cutoff (same fit as the search)",
        "disclosure": ["historical five-hour-cutoff replay, not a logged prospective forecast",
                       "timetable as of each cutoff not verified (present-day patterns)",
                       "fresh-test labels replayed from the public IRIS archive, as were training days"],
        "overall": {name: s["overall"] for name, s in report["scores"].items()},
        "current_unchanged": (Path(ML_MODELS_DIR) / "CURRENT").read_text().strip() == current_before,
    }
    (EXPERIMENT / "final.json").write_text(json.dumps(final, indent=1, default=float))
    for name, s in final["overall"].items():
        print(f"{name}: within-1 {s['within_1_min']:.4f}, within-3 {s['within_3_min']:.4f}, "
              f"within-5 {s['within_5_min']:.4f}, MAE {s['mae_scored']:.3f}, eligible {s['eligible']:,}, "
              f"missing {s['missing']}", flush=True)
    print(f"exclusions: {report['exclusions']}", flush=True)
    print(f"CURRENT unchanged: {final['current_unchanged']} -> {EXPERIMENT / 'final.json'}", flush=True)
    return 0 if final["current_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
