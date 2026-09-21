"""End-to-end training, on a dataset small enough to run in a test.

Asserts invariants, never exact losses: a test that pins a specific MAE breaks
whenever LightGBM changes a default, and tells you nothing when it does.

The manifest round-trip here is load-bearing. The first real training run got all
the way through fitting and calibration before dying on json.dumps, because
list() over a numpy array yields numpy.float64 and the derived `active` flag came
out numpy.bool_. Nothing before this test exercised the write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import Station, Train, Trip, TripStop

pytest.importorskip("lightgbm")
pytest.importorskip("numpy")

N_DATES = 20
N_TRAINS = 6
N_STATIONS = 4


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        for station_id in range(1, N_STATIONS + 1):
            s.add(Station(id=station_id, natural_key=f"s{station_id}",
                          name=f"Station {station_id}", lat=51.0 + station_id,
                          lon=7.0, tracked=1))
        for train_id in range(1, N_TRAINS + 1):
            s.add(Train(id=train_id, source=SOURCE_TT, identity_key=f"tts:RE/{train_id}",
                        category="RE", segment="RV", train_number=train_id, operator="DB",
                        display_name=f"RE {train_id}", n_stops=N_STATIONS,
                        first_departure_sec=6 * 3600 + train_id * 900))
        s.flush()
        trip_id = 0
        for day in range(N_DATES):
            date = f"2026-09-{day + 1:02d}"
            for train_id in range(1, N_TRAINS + 1):
                trip_id += 1
                s.add(Trip(id=trip_id, train_id=train_id, service_date=date,
                           source=SOURCE_TT, source_run_key=f"k{trip_id}",
                           status="COMPLETED", endpoints_tracked=1, n_polls_seen=1))
                s.flush()
                for station_id in range(1, N_STATIONS + 1):
                    base = 1789000000 + day * 86400 + train_id * 900 + station_id * 300
                    # A learnable signal: lateness rises with train id and with
                    # position along the route, plus a little per-day wobble.
                    delay = train_id * 60 + station_id * 30 + (day % 5) * 10
                    s.add(TripStop(
                        trip_id=trip_id, stop_sequence=station_id, station_id=station_id,
                        supposed_arrival_utc=base, supposed_departure_utc=base + 60,
                        actual_departure_utc=base + 60 + delay,
                        dep_delay_s=delay, arr_delay_s=delay,
                        quality="RECORDED", rule_version=1, cancelled=0,
                        arr_cancelled=0, dep_cancelled=0, added=0, is_handover=0,
                        planned_platform="1",
                    ))
        s.flush()
        yield s


@pytest.fixture
def quick(monkeypatch):
    """Ten rounds is plenty to prove the wiring; three hundred is not a test."""
    from dbrel.ml import train as train_module

    monkeypatch.setattr(train_module, "NUM_ROUNDS", 10)


def test_training_produces_a_scored_model(session, quick, tmp_path, monkeypatch):
    from dbrel.ml import artifacts
    from dbrel.ml.train import run_train

    monkeypatch.setattr(artifacts, "models_root", lambda: tmp_path)
    result = run_train(session, now=1_800_000_000)

    assert result.rows == N_DATES * N_TRAINS * N_STATIONS
    assert result.evaluation is not None
    assert result.evaluation.n > 0
    assert result.directory


def test_the_manifest_is_json_serialisable(session, quick, tmp_path, monkeypatch):
    """The bug that killed the first real run: numpy scalars reaching json.dumps.

    Reading it back through json is the assertion — a numpy.bool_ or a
    numpy.float64 in `metrics` raises on write, long after the model is fitted.
    """
    from dbrel.ml import artifacts
    from dbrel.ml.train import run_train

    monkeypatch.setattr(artifacts, "models_root", lambda: tmp_path)
    result = run_train(session, now=1_800_000_000)

    written = json.loads((Path(result.directory) / "manifest.json").read_text())
    assert isinstance(written["active"], bool)
    assert all(isinstance(v, (int, float)) for v in written["metrics"].values())
    assert written["feature_names"]


def test_the_split_never_lets_the_model_see_the_test_period(session, quick, tmp_path, monkeypatch):
    from dbrel.ml import artifacts
    from dbrel.ml.train import run_train

    monkeypatch.setattr(artifacts, "models_root", lambda: tmp_path)
    result = run_train(session, now=1_800_000_000)

    assert max(result.fit_dates) < min(result.calibrate_dates)
    assert max(result.calibrate_dates) < min(result.test_dates)


def test_a_model_that_loses_to_the_baseline_is_not_activated(session, quick, tmp_path, monkeypatch):
    """The guard that stops a worse model replacing the average it ships beside.

    Forced by setting the bar above 1.0, which nothing can clear.
    """
    from dbrel.ml import artifacts, train as train_module
    from dbrel.ml.train import run_train

    monkeypatch.setattr(artifacts, "models_root", lambda: tmp_path)
    monkeypatch.setattr(train_module, "ML_MIN_IMPROVEMENT", 2.0)
    result = run_train(session, now=1_800_000_000)

    assert result.activated is False
    assert "_rejected" in result.directory
    assert artifacts.latest(root=tmp_path) is None


def test_a_dry_run_writes_nothing(session, quick, tmp_path, monkeypatch):
    from dbrel.ml import artifacts
    from dbrel.ml.train import run_train

    monkeypatch.setattr(artifacts, "models_root", lambda: tmp_path)
    result = run_train(session, dry_run=True, now=1_800_000_000)

    assert result.evaluation is not None
    assert result.directory == ""
    assert not list(tmp_path.iterdir())


def test_too_few_dates_to_hold_out_is_reported_not_raised(quick, tmp_path, monkeypatch):
    from dbrel.ml import artifacts
    from dbrel.ml.train import run_train

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(artifacts, "models_root", lambda: tmp_path)
    with Session(engine, future=True) as empty:
        result = run_train(empty, now=1_800_000_000)
    assert result.evaluation is None
    assert "no usable observations" in result.reason
