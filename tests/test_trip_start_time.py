"""Deriving a trip's own origin start time for the stop_delay_average key.

`trip.source_run_key` for a Timetables ('tts') trip is `f"{daily_trip_id}-
{start_stamp}"`, and `daily_trip_id` is a signed 64-bit integer -- frequently
negative -- so it can itself contain a leading '-'. The parsing has to take
the *trailing* 10 characters, never split naively on '-', or a negative
daily_trip_id shifts the stamp.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.derive.stop_averages import build_stop_averages
from dbrel.models import ServiceLine, Station, StopDelayAverage, Train, Trip, TripStop

TZ = ZoneInfo("Europe/Berlin")
LINE_ID = 1
STATION_ID = 1


def _epoch(y, m, d, h, mi):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


@pytest.fixture
def engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(ServiceLine(id=LINE_ID, identity_key="RE/7", category="RE", line="RE7",
                          display_name="RE7", search_name="re7"))
        s.add(Station(id=STATION_ID, natural_key="a", name="Neumünster", lat=1, lon=1, state="DE-SH"))
        s.add(Train(id=1, identity_key="t1", line_id=LINE_ID, route_path="Flensburg|Neumünster|Hamburg-Altona",
                    origin_name="Flensburg", dest_name="Hamburg-Altona"))
        s.flush()

        # A negative daily_trip_id, exactly like real IRIS data -- the naive
        # "split on the last '-'" bug would slice this stamp wrong.
        s.add(Trip(id=1, train_id=1, service_date="2026-09-11", source="tts",
                   source_run_key="-1000366652095283523-2609110726", daily_trip_id="-1000366652095283523"))
        s.add(TripStop(
            id=1, trip_id=1, stop_sequence=2, station_id=STATION_ID,
            supposed_arrival_utc=_epoch(2026, 9, 11, 7, 30),
            supposed_departure_utc=_epoch(2026, 9, 11, 7, 31),
            arr_delay_s=30, dep_delay_s=30,
            quality="RECORDED", rule_version=1, cancelled=0, is_holiday=0,
        ))

        # A malformed run key (too short) -- must be excluded, not crash or
        # silently produce a bogus time.
        s.add(Trip(id=2, train_id=1, service_date="2026-09-12", source="tts",
                   source_run_key="bad", daily_trip_id="bad"))
        s.add(TripStop(
            id=2, trip_id=2, stop_sequence=2, station_id=STATION_ID,
            supposed_arrival_utc=_epoch(2026, 9, 12, 7, 30),
            supposed_departure_utc=_epoch(2026, 9, 12, 7, 31),
            arr_delay_s=999, dep_delay_s=999,
            quality="RECORDED", rule_version=1, cancelled=0, is_holiday=0,
        ))
        s.commit()
    return engine


def test_a_negative_daily_trip_id_does_not_shift_the_stamp(engine):
    with Session(engine, future=True) as s:
        report = build_stop_averages(engine, s)
        s.commit()
        rows = s.execute(select(StopDelayAverage)).scalars().all()
    row = next(r for r in rows if r.n_dep == 1)
    assert row.trip_start_time == "07:26"


def test_an_undecidable_start_time_is_excluded_and_reported(engine):
    with Session(engine, future=True) as s:
        report = build_stop_averages(engine, s)
        s.commit()
        rows = s.execute(select(StopDelayAverage)).scalars().all()
    assert report["excluded_no_start_time"] == 1
    # The malformed-run-key trip's 999s delay must not leak into any row.
    assert all(r.mean_dep_delay_s != 999 for r in rows if r.mean_dep_delay_s is not None)
