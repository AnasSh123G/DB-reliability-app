"""Delay-model features and dataset: leakage-free, and identical for training and prediction."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from dbrel.db import make_engine
from dbrel.holidays_de import load_holiday_dates
from dbrel.ml import features as F
from dbrel.ml.dataset import build_dataset, dataset_is_stale, load_dataset
from dbrel.ml.timetable import calls_at
from dbrel.models import Train, TripStop

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


def test_origin_terminus_and_unknown_rank():
    raw = {name: [0, 0, 0] for name in F.RAW_COLUMNS}
    raw.update(category=["RE"] * 3, operator=["DB"] * 3, state=["DE-NW"] * 3,
               stop_rank=[0, 4, np.nan], n_stops=[5, 5, np.nan], trip_start_sec=[0, 0, np.nan])
    out = F.compute_features(_one(raw))
    assert out["is_origin"].tolist()[:2] == [1.0, 0.0]
    assert out["is_terminus"].tolist()[:2] == [0.0, 1.0]
    assert np.isnan(out["stop_frac"][2]) and np.isnan(out["is_origin"][2])
    assert np.isnan(out["minutes_since_start"][2])  # unknown start -> unknown elapsed time


def test_unseen_categorical_value_encodes_as_unknown():
    feats = {"station": np.array([1, 2]), "train": np.array([5, 6]), "line": np.array([-1, 7]),
             "category": np.array(["ICE", "RE"], dtype=object), "operator": np.array(["DB", "DB"], dtype=object),
             "state": np.array(["", "DE-NW"], dtype=object), "weekday": np.array([0, 1]),
             "direction": np.array([0, 1])}
    vocab = F.build_vocabularies(feats)
    fresh = dict(feats, station=np.array([1, 99]), category=np.array(["ICE", "IC"], dtype=object))
    enc = F.encode_categoricals(fresh, vocab)
    assert enc["station"].tolist() == [0, F.UNKNOWN]
    assert enc["category"].tolist() == [vocab["category"].index("ICE"), F.UNKNOWN]
    assert enc["line"].tolist() == [0, 1]  # NO_LINE (-1) is a real category, not unknown


# --- direction -------------------------------------------------------------


def test_directions_for_line_and_lineless_trains(session):
    dirs = F.all_directions(session)
    assert dirs[70] != dirs[71]  # RE7 both ways
    # ICE 10 (Köln->Berlin) and 12 (Hannover->Berlin) run the same way along the
    # longest route on the stretch; ICE 11 runs back. Not all zero.
    assert dirs[10] == dirs[12] != dirs[11]
    for tid in (10, 11, 12, 70):
        assert F.direction_for(session, session.get(Train, tid)) == dirs[tid]


# --- dataset --------------------------------------------------------------


def test_dataset_rows_targets_and_meta(db, session, tmp_path):
    meta = build_dataset(db, tmp_path / "ml", F.all_directions(session))
    ds = load_dataset(tmp_path / "ml")
    n_calls = sum(len(p) for p in PATTERNS.values())
    assert meta["n_rows"] == n_calls * len(DATES)
    assert meta["dates"] == DATES
    # Origins have no arrival, termini no departure: that side is NaN, the other kept.
    assert np.isnan(ds.arr_min).sum() == 5 * len(DATES)
    assert np.isnan(ds.dep_min).sum() == 5 * len(DATES)
    assert set(ds.features) == set(F.FEATURES)
    assert not dataset_is_stale(meta, db)
    json.loads((tmp_path / "ml" / "dataset.json").read_text())


def test_cancelled_and_implausible_stops(db, session, tmp_path):
    first = session.query(TripStop).filter(TripStop.trip_id == 1).order_by(TripStop.stop_sequence).all()
    first[0].cancelled = 1  # cancelled: no target at all
    first[1].arr_delay_s = 3 * 3600  # implausible arrival, plausible departure
    first[1].dep_delay_s = 120
    session.commit()
    build_dataset(db, tmp_path / "ml", F.all_directions(session))
    ds = load_dataset(tmp_path / "ml")
    base = sum(len(p) for p in PATTERNS.values()) * len(DATES)
    assert len(ds.day) == base - 1
    kept = np.flatnonzero(ds.trip_id == 1)
    assert len(kept) == 2
    dortmund = ds.meta["vocab"]["station"].index(2)
    (middle,) = kept[ds.features["station"][kept] == dortmund]
    assert np.isnan(ds.arr_min[middle]) and ds.dep_min[middle] == pytest.approx(2.0)


def test_no_live_only_column_feeds_a_feature():
    assert not (set(F.RAW_COLUMNS) | set(F.FEATURES)) & F.LIVE_ONLY_COLUMNS


def test_stale_dataset_detected(db, session, tmp_path):
    meta = build_dataset(db, tmp_path / "ml", F.all_directions(session))
    session.query(TripStop).filter(TripStop.trip_id == 1).first().is_holiday = None
    session.commit()
    assert dataset_is_stale(meta, db)


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
        for name in F.FEATURES:
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
