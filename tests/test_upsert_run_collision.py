"""Two run_keys resolving to the same train on the same service_date must not crash.

Found live: a full-day /plan backfill (dbrel collect's cold start) raised
IntegrityError('UNIQUE constraint failed: trip.train_id, trip.service_date').
Trip carries two independent unique constraints -- (train_id, service_date) and
(source, source_run_key, service_date) -- and the trip upsert's ON CONFLICT
only ever named the second, so a second run_key resolving to the same train on
the same day hit the first one instead, as a plain insert with no conflict
handling.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.models import Station, Trip
from dbrel.timetables.parser import TtEvent, TtStop
from dbrel.timetables.runs import RunContext, RunCounters, tt_feed_version, upsert_run


def _stop(*, daily_trip_id: str, start_stamp: str, stop_index: int = 1) -> TtStop:
    dep = TtEvent(
        planned_ts=1_800_000_000,
        changed_ts=None,
        planned_platform="1",
        changed_platform=None,
        planned_status=None,
        changed_status=None,
        planned_path=None,
        changed_path=None,
        line=None,
        cancelled_at=None,
        hidden=False,
        wings=None,
        transition=None,
    )
    return TtStop(
        stop_id="123456",
        eva=8000041,
        daily_trip_id=daily_trip_id,
        start_stamp=start_stamp,
        stop_index=stop_index,
        category="RE",
        number="10422",
        owner="DB",
        trip_type=None,
        filter_flags=None,
        label=None,
        arrival=None,
        departure=dep,
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        fv_id = tt_feed_version(s)
        s.add(Station(id=1, natural_key="bochum@51.479,7.223", name="Bochum Hbf",
                      lat=51.479, lon=7.223, eva=8000041))
        s.flush()
        yield s, fv_id


def test_second_run_key_for_the_same_train_and_day_updates_rather_than_crashes(session):
    s, fv_id = session
    ctx = RunContext(fv_id=fv_id, station_id_by_eva={8000041: 1}, station_name_by_eva={8000041: "Bochum Hbf"},
                     train_by_key={})
    counters = RunCounters()

    # Same category/number/start_stamp (-> same identity_key, same service_date)
    # but a different daily_trip_id, so run_key differs between the two calls.
    first = _stop(daily_trip_id="123", start_stamp="2609160800")
    second = _stop(daily_trip_id="124", start_stamp="2609160800")
    assert first.run_key != second.run_key
    assert first.service_date == second.service_date

    trip_id_1 = upsert_run(s, ctx, counters, first.run_key, [first])
    s.flush()
    trip_id_2 = upsert_run(s, ctx, counters, second.run_key, [second])  # must not raise
    s.flush()

    assert trip_id_1 == trip_id_2, "both run_keys belong to the same train+service_date trip"
    assert s.scalar(select(Trip.id)) is not None
    rows = s.execute(select(Trip)).scalars().all()
    assert len(rows) == 1, "no second trip row should have been created"
