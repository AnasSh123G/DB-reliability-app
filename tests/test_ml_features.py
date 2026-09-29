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
from dbrel.ml.dataset import (
    build_arrival_dataset, build_dataset, dataset_is_stale, load_arrival_dataset, load_dataset,
)
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


def _doy(dates) -> tuple[np.ndarray, np.ndarray]:
    raw = {name: [0] * len(dates) for name in F.RAW_COLUMNS}
    raw.update(category=["RE"] * len(dates), operator=["DB"] * len(dates), state=[""] * len(dates),
               day_of_year_idx=[F.day_of_year_index(d) for d in dates])
    out = F.compute_features(_one(raw))
    return out["doy_sin"].astype(np.float64), out["doy_cos"].astype(np.float64)


def test_time_of_year_ignores_the_year():
    s, c = _doy(["2025-11-20", "2026-11-20", "2028-11-20"])
    assert s[0] == s[1] == s[2] and c[0] == c[1] == c[2]
    assert F.day_of_year_index("2026-03-01") == F.day_of_year_index("2028-03-01") == 60


def test_the_year_boundary_is_one_step_like_any_other_day():
    s, c = _doy(["2025-12-30", "2025-12-31", "2026-01-01", "2026-06-10", "2026-06-11"])
    step = np.hypot(np.diff(s), np.diff(c))
    assert step[1] == pytest.approx(step[0], rel=1e-5)  # 31 Dec -> 1 Jan
    assert step[1] == pytest.approx(step[3], rel=1e-5)  # like 10 -> 11 Jun


def test_non_leap_february_skips_the_29th():
    assert F.day_of_year_index("2026-03-01") - F.day_of_year_index("2026-02-28") == 2
    assert F.day_of_year_index("2028-03-01") - F.day_of_year_index("2028-02-29") == 1


def test_vectorised_index_matches_the_scalar_one():
    days = np.arange(np.datetime64("2025-10-01"), np.datetime64("2029-01-02")).astype(np.int64)
    expected = [F.day_of_year_index(str(np.datetime64(int(d), "D"))) for d in days]
    assert F.day_of_year_indices(days).tolist() == expected


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
    assert set(ds.features) == set(F.FEATURES) | set(F.SEASONAL)
    assert not dataset_is_stale(meta, db)
    json.loads((tmp_path / "ml" / "dataset.json").read_text())


def test_arrival_view_preserves_individual_targets_and_v2_cache(db, session, tmp_path):
    out = tmp_path / "ml"
    dirs = F.all_directions(session)
    build_dataset(db, out, dirs)
    before = (out / "dataset.npz").read_bytes()
    for day, minutes in enumerate((1, 4, 12)):
        trip = day * len(PATTERNS) + list(PATTERNS).index(70) + 1
        stop = session.query(TripStop).filter_by(trip_id=trip, stop_sequence=20).one()
        stop.arr_delay_s = minutes * 60
    cancelled = session.query(TripStop).filter_by(trip_id=4, stop_sequence=20).one()
    cancelled.arr_cancelled = 1
    session.commit()
    meta = build_arrival_dataset(db, out, dirs)
    ds = load_arrival_dataset(out)
    assert (out / "dataset.npz").read_bytes() == before
    assert meta["kind"] == "arrival-only" and meta["timetable_asof_verified"] is False
    assert len(ds.arr_min) == meta["n_arr_targets"]
    assert np.isnan(ds.dep_min).all()
    assert np.unique(ds.stop_id).size == len(ds.stop_id)
    assert np.all(ds.cutoff_utc == ds.scheduled_arrival_utc - 5 * 3600)
    for day, minutes in enumerate((1, 4, 12)):
        trip = day * len(PATTERNS) + list(PATTERNS).index(70) + 1
        (idx,) = np.flatnonzero((ds.trip_id == trip) & (ds.stop_sequence == 20))
        assert ds.arr_min[idx] == minutes
        assert ds.features["sched_min"][idx] == 8 * 60 + 50  # arrival, not 08:52 departure
        assert ds.v2_sched_local_sec[idx] % 86400 == (8 * 60 + 52) * 60  # the v2 model's clock
    assert not np.any((ds.trip_id == 4) & (ds.stop_sequence == 20))


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


def test_incumbent_inputs_equal_the_v2_datasets_own_features(db, session, tmp_path):
    from dbrel.ml.arrival import incumbent_features

    out = tmp_path / "ml"
    dirs = F.all_directions(session)
    build_dataset(db, out, dirs)
    build_arrival_dataset(db, out, dirs)
    v2, arr = load_dataset(out), load_arrival_dataset(out)
    station_v2 = np.array(v2.meta["vocab"]["station"])[v2.features["station"]]
    station_arr = np.array(arr.meta["vocab"]["station"])[arr.features["station"]]
    index = {(t, s): i for i, (t, s) in enumerate(zip(v2.trip_id.tolist(), station_v2.tolist()))}
    match = np.array([index[(t, s)] for t, s in zip(arr.trip_id.tolist(), station_arr.tolist())])
    rows = np.arange(len(arr.arr_min))
    got = incumbent_features(arr, rows, v2.meta["vocab"])
    for name in F.FEATURES:
        np.testing.assert_array_equal(got[name], v2.features[name][match], err_msg=name)
