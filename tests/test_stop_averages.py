"""The per-stop aggregate: what pools together, and what must not.

Two grouping rules carry this table, and neither is visible in the output:
Monday-to-Saturday pooling with Sundays and holidays held apart, and the scheduled
time being part of the key. Both are asserted here rather than trusted.
"""

from __future__ import annotations

import time
from datetime import date

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.derive.stop_averages import run_stop_averages
from dbrel.gtfs.timeutil import gtfs_sec_to_epoch
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    DAY_TYPE_SUNDAY_HOLIDAY,
    DAY_TYPE_WEEKDAY,
    QUALITY_CANCELLED,
    QUALITY_NO_DATA,
    QUALITY_RECORDED,
    ServiceLine,
    Station,
    Train,
    TrainStationDelay,
    TrainStopAverage,
    Trip,
    TripStop,
)
from dbrel.views import create_average_views

STATION_ID = 1
OTHER_STATION_ID = 2
TRAIN_ID = 1
LINE_ID = 1
NOW = int(time.time())

# 11:00 and 11:41 as service-day seconds. The second is the "same number, later on
# Saturday" case that the scheduled time in the key exists to separate.
ELEVEN = 11 * 3600
ELEVEN_FORTYONE = 11 * 3600 + 41 * 60

# Every date below is chosen for its weekday; see test_the_fixture_dates_are_the_weekdays_claimed.
MON, TUE, WED = "2026-09-14", "2026-09-15", "2026-09-16"
THU, FRI, SAT = "2026-09-17", "2026-09-18", "2026-09-19"
SUN, SUN2 = "2026-09-20", "2026-09-27"
ASCENSION = "2026-05-14"  # a Thursday, and a public holiday
UNITY_DAY = "2026-10-03"  # a Saturday, and a public holiday


@pytest.fixture
def engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    with Session(engine, future=True) as s:
        s.add(ServiceLine(id=LINE_ID, identity_key="NX/RE1", category="NX", line="RE1",
                          display_name="RE1", search_name="re1"))
        s.add(Station(id=STATION_ID, natural_key="bochum@51.478,7.223", name="Bochum Hbf",
                      lat=51.478, lon=7.223))
        s.add(Station(id=OTHER_STATION_ID, natural_key="essen@51.451,7.014", name="Essen Hbf",
                      lat=51.451, lon=7.014))
        s.add(Train(id=TRAIN_ID, source=SOURCE_TT, identity_key="tts:NX/26839",
                    category="NX", segment="RV", line_id=LINE_ID, display_name="RE1 26839"))
        s.flush()
        yield s


def add_stop(
    session,
    service_date: str,
    *,
    sched_sec: int = ELEVEN,
    station_id: int = STATION_ID,
    stop_sequence: int = 3,
    dep_delay: int | None = 60,
    arr_delay: int | None = 60,
    terminus: bool = False,
    source: str = SOURCE_TT,
    quality: str = QUALITY_RECORDED,
    observed_at: int | None = NOW,
):
    """One trip calling once, scheduled at `sched_sec` on `service_date`.

    A terminus stop has an arrival and no departure, which is what forces the key
    to fall back from the departure time to the arrival time.
    """
    day = date.fromisoformat(service_date)
    trip = session.scalar(
        select(Trip).where(Trip.train_id == TRAIN_ID, Trip.service_date == service_date)
    )
    if trip is None:
        trip = Trip(train_id=TRAIN_ID, service_date=service_date, source=source,
                    source_run_key=f"{source}:{service_date}:{sched_sec}", status="COMPLETED")
        session.add(trip)
        session.flush()
    session.add(TripStop(
        trip_id=trip.id,
        stop_sequence=stop_sequence,
        station_id=station_id,
        supposed_arrival_utc=gtfs_sec_to_epoch(day, sched_sec - 60),
        supposed_departure_utc=None if terminus else gtfs_sec_to_epoch(day, sched_sec),
        arr_delay_s=arr_delay,
        dep_delay_s=None if terminus else dep_delay,
        quality=quality,
        rule_version=1,
        is_handover=0,
        last_observed_at=observed_at,
    ))
    session.flush()
    return trip


def rows(session) -> list[TrainStopAverage]:
    run_stop_averages(session, full=True)
    return list(
        session.execute(
            select(TrainStopAverage).order_by(
                TrainStopAverage.day_type, TrainStopAverage.sched_sec
            )
        ).scalars()
    )


def test_the_fixture_dates_are_the_weekdays_claimed():
    """Guard: every grouping test below is meaningless if these drift."""
    assert [date.fromisoformat(d).weekday() for d in (MON, TUE, WED, THU, FRI, SAT)] == [
        0, 1, 2, 3, 4, 5
    ]
    assert date.fromisoformat(SUN).weekday() == 6
    assert date.fromisoformat(SUN2).weekday() == 6
    assert date.fromisoformat(ASCENSION).weekday() == 3
    assert date.fromisoformat(UNITY_DAY).weekday() == 5


class TestPooling:
    def test_all_weekdays_pool_into_one_row(self, session):
        """The whole point: Monday and Wednesday are the same service."""
        for day in (MON, TUE, WED, THU, FRI):
            add_stop(session, day)
        result = rows(session)
        assert len(result) == 1
        assert result[0].day_type == DAY_TYPE_WEEKDAY
        assert result[0].n_dep == 5
        assert result[0].n_runs == 5

    def test_saturday_joins_the_weekday_row(self, session):
        add_stop(session, MON)
        add_stop(session, SAT)
        result = rows(session)
        assert len(result) == 1
        assert result[0].n_dep == 2

    def test_sunday_gets_its_own_row(self, session):
        add_stop(session, MON)
        add_stop(session, SUN)
        result = rows(session)
        assert [r.day_type for r in result] == [DAY_TYPE_SUNDAY_HOLIDAY, DAY_TYPE_WEEKDAY]
        assert all(r.n_dep == 1 for r in result)

    def test_sundays_pool_with_each_other(self, session):
        add_stop(session, SUN)
        add_stop(session, SUN2)
        result = rows(session)
        assert len(result) == 1
        assert result[0].day_type == DAY_TYPE_SUNDAY_HOLIDAY
        assert result[0].n_dep == 2

    def test_a_midweek_holiday_lands_with_the_sundays(self, session):
        """Ascension is a Thursday. It must not touch the weekday figure."""
        add_stop(session, THU, dep_delay=60)
        add_stop(session, ASCENSION, dep_delay=600)
        result = rows(session)
        by_type = {r.day_type: r for r in result}
        assert by_type[DAY_TYPE_WEEKDAY].n_dep == 1
        assert by_type[DAY_TYPE_WEEKDAY].avg_dep_delay_s == 60
        assert by_type[DAY_TYPE_SUNDAY_HOLIDAY].avg_dep_delay_s == 600

    def test_a_holiday_on_a_saturday_leaves_the_weekday_row(self, session):
        add_stop(session, SAT)
        add_stop(session, UNITY_DAY)
        by_type = {r.day_type: r for r in rows(session)}
        assert by_type[DAY_TYPE_WEEKDAY].n_dep == 1
        assert by_type[DAY_TYPE_SUNDAY_HOLIDAY].n_dep == 1


class TestScheduledTimeIsPartOfTheKey:
    def test_two_departure_times_stay_two_rows(self, session):
        """The same train number running a different Saturday timetable.

        Pooling these would produce a mean describing neither service.
        """
        add_stop(session, MON, sched_sec=ELEVEN, dep_delay=60)
        add_stop(session, TUE, sched_sec=ELEVEN, dep_delay=60)
        add_stop(session, SAT, sched_sec=ELEVEN_FORTYONE, dep_delay=900)

        result = rows(session)
        assert len(result) == 2
        early, late = result[0], result[1]
        assert early.sched_sec == ELEVEN and early.n_dep == 2
        assert late.sched_sec == ELEVEN_FORTYONE and late.n_dep == 1
        assert late.avg_dep_delay_s == 900

    def test_the_same_time_on_two_dates_is_one_row(self, session):
        add_stop(session, MON, sched_sec=ELEVEN)
        add_stop(session, TUE, sched_sec=ELEVEN)
        assert len(rows(session)) == 1

    def test_a_terminus_keys_on_its_arrival(self, session):
        """No departure at all, so the departure time cannot be the key.

        A NULL key column would make SQLite treat every run as a new row.
        """
        add_stop(session, MON, sched_sec=ELEVEN, terminus=True)
        add_stop(session, TUE, sched_sec=ELEVEN, terminus=True)
        result = rows(session)
        assert len(result) == 1
        assert result[0].sched_kind == "ARR"
        assert result[0].sched_dep_sec is None
        assert result[0].sched_sec == result[0].sched_arr_sec == ELEVEN - 60
        assert result[0].n_dep == 0
        assert result[0].n_arr == 2

    def test_a_through_stop_keys_on_its_departure(self, session):
        add_stop(session, MON, sched_sec=ELEVEN)
        row = rows(session)[0]
        assert row.sched_kind == "DEP"
        assert row.sched_sec == row.sched_dep_sec == ELEVEN
        assert row.sched_arr_sec == ELEVEN - 60

    def test_an_after_midnight_stop_keeps_going_past_86400(self, session):
        """GTFS service-day seconds, not a wall clock — 00:30 is 24:30."""
        add_stop(session, MON, sched_sec=24 * 3600 + 1800)
        assert rows(session)[0].sched_sec == 88200


class TestRowContents:
    def test_a_row_identifies_train_line_and_stop(self, session):
        add_stop(session, MON)
        row = rows(session)[0]
        assert row.train_id == TRAIN_ID
        assert row.line_id == LINE_ID
        assert row.station_id == STATION_ID
        assert row.segment == "RV"
        assert row.stop_sequence == 3

    def test_a_journey_reads_in_order(self, session):
        add_stop(session, MON, sched_sec=ELEVEN, station_id=STATION_ID, stop_sequence=3)
        add_stop(session, MON, sched_sec=ELEVEN + 1200, station_id=OTHER_STATION_ID,
                 stop_sequence=7)
        journey = rows(session)
        assert [r.stop_sequence for r in journey] == [3, 7]
        assert [r.station_id for r in journey] == [STATION_ID, OTHER_STATION_ID]

    def test_only_recorded_delays_are_averaged(self, session):
        add_stop(session, MON, dep_delay=60)
        add_stop(session, TUE, dep_delay=None, arr_delay=None, quality=QUALITY_NO_DATA)
        row = rows(session)[0]
        assert row.n_dep == 1
        assert row.avg_dep_delay_s == 60
        # NO_DATA is an unknown, so it is not even in the cancellation denominator.
        assert row.n_known == 1

    def test_cancellations_are_counted_apart_not_averaged_as_zero(self, session):
        add_stop(session, MON, dep_delay=60)
        add_stop(session, TUE, dep_delay=None, arr_delay=None, quality=QUALITY_CANCELLED)
        row = rows(session)[0]
        assert row.n_dep == 1
        assert row.avg_dep_delay_s == 60
        assert row.n_cancelled == 1
        assert row.n_known == 2


class TestReconciliation:
    def test_a_group_that_loses_its_samples_loses_its_row(self, session):
        add_stop(session, MON, sched_sec=ELEVEN)
        add_stop(session, SUN, sched_sec=ELEVEN)
        assert len(rows(session)) == 2

        session.execute(
            text(
                "DELETE FROM trip_stop WHERE trip_id IN"
                " (SELECT id FROM trip WHERE service_date = :d)"
            ),
            {"d": SUN},
        )
        session.flush()

        remaining = rows(session)
        assert len(remaining) == 1
        assert remaining[0].day_type == DAY_TYPE_WEEKDAY

    def test_rerunning_updates_rather_than_duplicates(self, session):
        add_stop(session, MON)
        rows(session)
        add_stop(session, TUE)
        result = rows(session)
        assert len(result) == 1
        assert result[0].n_dep == 2


class TestViews:
    def test_the_two_views_partition_the_table(self, engine, session):
        create_average_views(engine)
        for day in (MON, SAT, SUN, ASCENSION):
            add_stop(session, day)
        rows(session)
        session.commit()

        total = session.scalar(select(func.count()).select_from(TrainStopAverage))
        weekday = session.scalar(text("SELECT COUNT(*) FROM avg_weekday"))
        sunday = session.scalar(text("SELECT COUNT(*) FROM avg_sunday_holiday"))
        assert total == 2
        assert weekday == 1
        assert sunday == 1
        assert weekday + sunday == total

    def test_each_view_returns_only_its_day_type(self, engine, session):
        create_average_views(engine)
        add_stop(session, MON)
        add_stop(session, SUN)
        rows(session)
        session.commit()

        assert session.execute(
            text("SELECT DISTINCT day_type FROM avg_weekday")
        ).scalars().all() == [DAY_TYPE_WEEKDAY]
        assert session.execute(
            text("SELECT DISTINCT day_type FROM avg_sunday_holiday")
        ).scalars().all() == [DAY_TYPE_SUNDAY_HOLIDAY]


def test_the_other_aggregate_is_left_alone(session):
    """train_stop_average is additive — it must not touch train_station_delay."""
    add_stop(session, MON)
    rows(session)
    assert session.scalar(select(func.count()).select_from(TrainStationDelay)) == 0
