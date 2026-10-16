"""Compare planner forecasts with browser-encoded ONNX inputs; use capped launcher."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

import numpy as np
import onnxruntime as ort
from sqlalchemy.orm import Session

from dbrel.config import DB_PATH, TZ
from dbrel.db import make_engine
from dbrel.ml.bakeoff import current_artifact
from dbrel.models import Train
from dbrel.planner.constants import NO_TIME
from dbrel.planner.forecast import ModelForecaster
from dbrel.planner.profile import day_key
from dbrel.planner.timetable import get_timetable
from dbrel.web import queries


def sample_runs(tt, today, count=50):
    """Reservoir sample valid runs without materializing the national candidate set."""
    monday = today + timedelta(days=(-today.weekday()) % 7 or 7)
    rng, selected, seen = random.Random(0), [], 0
    for variant in range(len(tt.var_train)):
        base = int(tt.var_first[variant])
        if int(tt.var_cut[variant]) < 2:
            continue
        station = int(tt.stop_si[base])
        for offset in range(7):
            day = monday + timedelta(days=offset)
            key = day_key(day, tt.station_state[station], tt.holiday_set)
            if not ((int(tt.var_days[variant]) >> key) & 1):
                continue
            seen += 1
            run = (variant, day)
            if len(selected) < count:
                selected.append(run)
            else:
                index = rng.randrange(seen)
                if index < count:
                    selected[index] = run
    if len(selected) < count:
        raise RuntimeError(f"Need {count} valid runs, found {len(selected)}")
    return selected


def compare_run(session, tt, forecaster, onnx, manifest, direction, run):
    variant, day = run
    forecasts = forecaster.forecast([run])[run]
    train = session.get(Train, int(tt.var_train[variant]))
    base = int(tt.var_first[variant])
    largest, checked, excluded = 0.0, 0, 0
    for position in range(int(tt.var_len[variant])):
        row = base + position
        if tt.stop_arr[row] == NO_TIME:
            continue
        if position >= int(tt.var_cut[variant]):
            excluded += 1
            continue
        encoded = queries._model_input(
            session,
            manifest,
            train=train,
            station_id=int(tt.station_id[tt.stop_si[row]]),
            service_date=day,
            direction=direction.get(session, train),
            holiday_set=tt.holiday_set,
            arr_sec=int(tt.stop_arr[row]),
            dep_sec=None if tt.stop_dep[row] == NO_TIME else int(tt.stop_dep[row]),
            variant=tt.var_path[variant],
        )
        if encoded is None:
            raise AssertionError(f"Browser input missing for {run} position={position}")
        output = onnx.run(
            ["quantiles"],
            {
                "cat": np.asarray([encoded["cat"]], dtype=np.int64),
                "num": np.asarray([encoded["num"]], dtype=np.float32),
            },
        )[0]
        planner, browser = float(forecasts[position, 1]), float(output[0, 1])
        if not np.isfinite(planner) or not np.isfinite(browser):
            raise AssertionError(f"Nonfinite forecast for {run} position={position}")
        largest = max(largest, abs(planner - browser))
        checked += 1
    return largest, checked, excluded


def main():
    model = queries.current_model()
    artifact = current_artifact(queries.ML_MODELS_DIR)
    if model is None or artifact is None:
        raise RuntimeError("No current delay model available")
    stamp, manifest = model
    engine = make_engine(DB_PATH)
    try:
        with Session(engine) as session:
            tt = get_timetable(session)
            runs = sample_runs(tt, datetime.now(TZ).date())
            forecaster = ModelForecaster(tt, session, artifact, stamp)
            direction = queries._ModelDirectionCache()
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            onnx = ort.InferenceSession(
                str(artifact / "model.onnx"),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            largest, checked, excluded = 0.0, 0, 0
            for run in runs:
                error, n_rows, n_excluded = compare_run(
                    session, tt, forecaster, onnx, manifest, direction, run
                )
                largest, checked, excluded = (
                    max(largest, error),
                    checked + n_rows,
                    excluded + n_excluded,
                )
                print(
                    f"Run variant={run[0]} service_date={run[1]} arrivals={n_rows} max_abs_min={error:.8f}",
                    flush=True,
                )
            report = {
                "model_stamp": stamp,
                "seed": 0,
                "runs": len(runs),
                "arrival_rows": checked,
                "faulty_schedule_arrivals_excluded": excluded,
                "max_abs_min": largest,
                "tolerance_min": 0.01,
                "passed": checked > 0 and largest <= 0.01,
            }
            print("PARITY " + json.dumps(report), flush=True)
            if not report["passed"]:
                raise AssertionError(f"Planner/browser parity failed: {report}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
