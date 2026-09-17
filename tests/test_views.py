"""The seven day-of-week views.

The whole point of these is the weekday numbering, which is wrong in two different
directions depending on which side you read it from: SQLite's strftime('%w') calls
Sunday 0, Python's date.weekday() calls Monday 0. If the conversion between them
ever slips, every view silently returns the wrong day's trains -- data that looks
entirely plausible and is off by one. So it is pinned against real dates here.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import QUALITY_RECORDED, Station, Train, Trip, TripStop
from dbrel.views import DAY_NAMES, create_day_views, sqlite_dow_expr

STATION_ID = 1
TRAIN_ID = 1

# A week of known weekdays, Monday through Sunday.
WEEK = {
    "2026-09-07": "monday",
    "2026-09-08": "tuesday",
    "2026-09-09": "wednesday",
    "2026-09-10": "thursday",
    "2026-09-11": "friday",
    "2026-09-12": "saturday",
    "2026-09-13": "sunday",
}


@pytest.fixture
def engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    create_day_views(engine)
    with Session(engine, future=True) as s:
        s.add(Station(id=STATION_ID, natural_key="bochum@51.478,7.223",
                      name="Bochum Hbf", lat=51.478, lon=7.223))
        s.add(Train(id=TRAIN_ID, source=SOURCE_TT, identity_key="tts:RE/10422",
                    category="RE", segment="RV", display_name="RE 10422"))
        s.flush()
        for service_date in WEEK:
            trip = Trip(train_id=TRAIN_ID, service_date=service_date, source=SOURCE_TT,
                        source_run_key=f"tts:{service_date}", status="COMPLETED")
            s.add(trip)
            s.flush()
            s.add(TripStop(trip_id=trip.id, stop_sequence=1, station_id=STATION_ID,
                           arr_delay_s=60, dep_delay_s=60, quality=QUALITY_RECORDED,
                           rule_version=1, is_handover=0))
        s.commit()
    return engine


def test_the_fixture_dates_really_are_those_weekdays():
    """Guard the guard: if these dates are wrong every assertion below is vacuous."""
    for service_date, name in WEEK.items():
        assert DAY_NAMES[date.fromisoformat(service_date).weekday()] == name


def test_each_view_returns_only_its_own_day(engine):
    with engine.connect() as connection:
        for service_date, name in WEEK.items():
            rows = connection.execute(
                text(f"SELECT service_date FROM day_{name}")
            ).scalars().all()
            assert rows == [service_date], f"day_{name} must hold {service_date} alone"


def test_the_seven_views_partition_the_facts(engine):
    """Every trip_stop appears in exactly one view -- no gaps, no double counting."""
    with engine.connect() as connection:
        total = connection.execute(text("SELECT COUNT(*) FROM trip_stop")).scalar_one()
        covered = sum(
            connection.execute(text(f"SELECT COUNT(*) FROM day_{name}")).scalar_one()
            for name in DAY_NAMES
        )
        assert covered == total == len(WEEK)


def test_python_and_sqlite_weekday_numbering_are_converted_not_assumed(engine):
    """date.weekday() is 0=Monday; strftime('%w') is 0=Sunday."""
    assert sqlite_dow_expr(0) == 1, "Python Monday is SQLite 1"
    assert sqlite_dow_expr(6) == 0, "Python Sunday is SQLite 0"

    with engine.connect() as connection:
        for service_date in WEEK:
            sqlite_dow = connection.execute(
                text("SELECT CAST(strftime('%w', :d) AS INTEGER)"), {"d": service_date}
            ).scalar_one()
            python_dow = date.fromisoformat(service_date).weekday()
            assert sqlite_dow_expr(python_dow) == sqlite_dow


def test_views_survive_being_created_twice(engine):
    """create_all() runs on every cold start, so this must not raise."""
    create_day_views(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM day_monday")).scalar_one() == 1
