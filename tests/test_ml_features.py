"""Delay-model features and dataset: leakage-free, and identical for training and prediction."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from dbrel.db import make_engine
from dbrel.holidays_de import load_holiday_dates
from dbrel.ml import features as F
from dbrel.ml.dataset import (
    build_dataset, load_dataset,
)
from dbrel.ml.timetable import calls_at
from dbrel.models import Train

from ml_fixture import DATES, PATTERNS, build


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "dbrel.sqlite"
    build(path)
    return path


@pytest.fixture
def session(db):
    engine = make_engine(db)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _one(raw: dict) -> dict:
    return {k: np.array(v, dtype=object if k in ("category", "operator", "state") else np.float64)
            for k, v in raw.items()}


# --- compute_features --------------------------------------------------------


def test_time_and_route_position_features():
    raw = {name: [0] for name in F.RAW_COLUMNS}
    raw.update(category=["ICE"], operator=["DB"], state=[None], lat=[52.0], lon=[9.0],
               sched_local_sec=[26 * 3600 + 120], trip_start_sec=[23 * 3600],
               stop_rank=[1], n_stops=[3], direction=[1])
    out = F.compute_features(_one(raw))
    assert out["sched_min"][0] == 2 * 60 + 2  # 26:02 service-day time is 02:02 local
    assert out["trip_start_min"][0] == 23 * 60
    assert out["minutes_since_start"][0] == 3 * 60 + 2  # wraps past midnight
    assert out["stop_frac"][0] == pytest.approx(0.5)
    assert out["is_origin"][0] == 0 and out["is_terminus"][0] == 0
    assert out["state"][0] == ""


def _doy(dates) -> tuple[np.ndarray, np.ndarray]:
    raw = {name: [0] * len(dates) for name in F.RAW_COLUMNS}
    raw.update(category=["RE"] * len(dates), operator=["DB"] * len(dates), state=[""] * len(dates),
               day_of_year_idx=[F.day_of_year_index(d) for d in dates])
    out = F.compute_features(_one(raw))
    return out["doy_sin"].astype(np.float64), out["doy_cos"].astype(np.float64)


# --- direction -------------------------------------------------------------


# --- dataset --------------------------------------------------------------


def test_no_live_only_column_feeds_a_feature():
    assert not (set(F.RAW_COLUMNS) | set(F.FEATURES)) & F.LIVE_ONLY_COLUMNS


# --- training row == prediction row ------------------------------------------


def test_training_and_prediction_build_identical_rows(db, session, tmp_path):
    build_dataset(db, tmp_path / "ml", F.all_directions(session))
    ds = load_dataset(tmp_path / "ml")
    vocab = ds.meta["vocab"]

    # ICE 10 at Hannover on DATES[2]: crosses midnight, unresolved-state neighbour.
    for train_id, station_id in ((10, 4), (70, 2), (71, 1)):
        trip_id = 2 * len(PATTERNS) + list(PATTERNS).index(train_id) + 1  # trips are numbered day by day
        row = np.flatnonzero((ds.trip_id == trip_id) &
                             (ds.features["station"] == vocab["station"].index(station_id)))
        assert len(row) == 1
        train = session.get(Train, train_id)
        (call,) = calls_at(session, train, station_id, date.fromisoformat(DATES[2]))
        raw = call.raw(direction=F.direction_for(session, train), holiday_set=load_holiday_dates(session))
        predicted = F.encode_categoricals(F.compute_features(_one(raw)), vocab)
        for name in F.FEATURES + F.SEASONAL:
            a, b = float(ds.features[name][row[0]]), float(predicted[name][0])
            assert (np.isnan(a) and np.isnan(b)) or a == b, (train_id, name, a, b)


def test_prediction_side_never_reads_history_or_averages(session):
    statements: list[str] = []
    engine = session.get_bind()

    def capture(conn, cursor, statement, *args):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        train = session.get(Train, 10)
        calls = calls_at(session, train, 4, date(2026, 9, 3))
        calls[0].raw(direction=F.direction_for(session, train), holiday_set=load_holiday_dates(session))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements
    for forbidden in ("trip_stop", "stop_delay_average", "train_stop_delay_average", " trip "):
        assert not any(forbidden in s for s in statements), forbidden


