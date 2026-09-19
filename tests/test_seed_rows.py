"""What the seeder actually writes.

The distribution is checked in test_seed.py; this checks the rows carry the
provenance and date separation that keep invented data distinguishable from
measured data.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_SYNTH, SOURCE_TT
from dbrel.models import QUALITY_CANCELLED, QUALITY_RECORDED, Station, StationStop, Train, Trip, TripStop
from dbrel.seed import _runs_on, clear_synthetic, seed_synthetic
from dbrel.timetables.runs import tt_feed_version


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        fv_id = tt_feed_version(s)
        s.add(Station(id=1, natural_key="bochum@51.478,7.223", name="Bochum Hbf",
                      lat=51.478, lon=7.223, tracked=1, eva=8000041))
        s.add(Station(id=2, natural_key="essen@51.451,7.014", name="Essen Hbf",
                      lat=51.451, lon=7.014, tracked=1, eva=8000098))
        s.add(Train(id=1, source=SOURCE_TT, identity_key="tts:RE/10422", category="RE",
                    segment="RV", display_name="RE 10422"))
        s.flush()
        # A through call at Bochum, then a terminus at Essen.
        s.add(StationStop(feed_version_id=fv_id, train_id=1, stop_sequence=1, station_id=1,
                          planned_arrival_sec=8 * 3600, planned_departure_sec=8 * 3600 + 120,
                          platform_code="4"))
        s.add(StationStop(feed_version_id=fv_id, train_id=1, stop_sequence=2, station_id=2,
                          planned_arrival_sec=8 * 3600 + 1500, planned_departure_sec=None))
        s.flush()
        yield s


def _expected_runs(days: int, seed: int, train_id: int = 1) -> int:
    """How many of the `days` dates this train is timetabled on.

    Not simply `days`: DOW_SERVICE_RATE drops a share of services at weekends, so
    the count depends on which weekdays the window happens to cover.
    """
    last = date.today() - timedelta(days=1)
    first = last - timedelta(days=days - 1)
    return sum(
        1
        for offset in range(days)
        if _runs_on(seed, train_id, (first + timedelta(days=offset)).weekday())
    )


def test_seeds_the_requested_number_of_days(session):
    expected = _expected_runs(10, seed=1)
    result = seed_synthetic(session, days=10, seed=1)
    assert result.trips_written == expected
    assert result.stops_written == expected * 2  # two stops per run
    assert session.scalar(select(func.count()).select_from(Trip)) == expected


def test_every_seeded_row_is_tagged_synthetic(session):
    seed_synthetic(session, days=5, seed=1)
    sources = session.execute(select(Trip.source).distinct()).scalars().all()
    assert sources == [SOURCE_SYNTH]


def test_seeded_dates_sit_strictly_before_any_real_run(session):
    """uq_trip_train_date would otherwise collide with a real run."""
    real_date = date.today() - timedelta(days=3)
    session.add(Trip(train_id=1, service_date=real_date.isoformat(), source=SOURCE_TT,
                     source_run_key="tts:real", status="COMPLETED"))
    session.flush()

    seed_synthetic(session, days=5, seed=1)
    seeded = session.execute(
        select(Trip.service_date).where(Trip.source == SOURCE_SYNTH)
    ).scalars().all()
    assert seeded, "the seeder must still find room"
    assert max(seeded) < real_date.isoformat()


def test_actual_times_are_consistent_with_the_delays(session):
    seed_synthetic(session, days=6, seed=7)
    rows = session.execute(
        select(TripStop).where(TripStop.quality == QUALITY_RECORDED)
    ).scalars().all()
    assert rows
    for stop in rows:
        if stop.dep_delay_s is not None:
            assert stop.actual_departure_utc == stop.supposed_departure_utc + stop.dep_delay_s
        if stop.arr_delay_s is not None:
            assert stop.actual_arrival_utc == stop.supposed_arrival_utc + stop.arr_delay_s


def test_cancelled_runs_carry_no_delay(session):
    seed_synthetic(session, days=200, seed=3)  # enough days to hit the 1.5% cancel rate
    cancelled = session.execute(
        select(TripStop).where(TripStop.quality == QUALITY_CANCELLED)
    ).scalars().all()
    assert cancelled, "200 days at a 1.5% rate should produce some cancellations"
    for stop in cancelled:
        assert stop.dep_delay_s is None and stop.arr_delay_s is None
        assert stop.cancelled == 1


def test_terminus_stop_has_no_departure(session):
    seed_synthetic(session, days=4, seed=1)
    terminus = session.execute(
        select(TripStop).where(TripStop.station_id == 2)
    ).scalars().all()
    assert terminus
    for stop in terminus:
        assert stop.supposed_departure_utc is None
        assert stop.dep_delay_s is None
        assert stop.supposed_arrival_utc is not None


def test_reseeding_is_a_no_op(session):
    first = seed_synthetic(session, days=6, seed=1)
    before = session.scalar(select(func.count()).select_from(TripStop))
    seed_synthetic(session, days=6, seed=1)
    after = session.scalar(select(func.count()).select_from(TripStop))
    assert after == before, "a second run must not duplicate anything"
    assert first.trips_written == _expected_runs(6, seed=1)


def test_clear_removes_everything_it_wrote(session):
    seed_synthetic(session, days=6, seed=1)
    clear_synthetic(session)
    assert session.scalar(select(func.count()).select_from(Trip)) == 0
    assert session.scalar(select(func.count()).select_from(TripStop)) == 0


def test_clear_leaves_real_runs_alone(session):
    session.add(Trip(train_id=1, service_date="2099-01-01", source=SOURCE_TT,
                     source_run_key="tts:real", status="COMPLETED"))
    session.flush()
    seed_synthetic(session, days=6, seed=1)
    clear_synthetic(session)

    remaining = session.execute(select(Trip.source)).scalars().all()
    assert remaining == [SOURCE_TT]
