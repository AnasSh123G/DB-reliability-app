"""The averaging pass, against a real schema in memory.

The filters are the point. Each one excludes rows that would otherwise bias the
number in a specific direction, and none of them is visible in the output — so
they are asserted here rather than trusted.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.derive._common import percentile as _percentile
from dbrel.derive.delay_averages import run_delay_averages
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    QUALITY_CANCELLED,
    QUALITY_NO_DATA,
    QUALITY_PREDICTION_ONLY,
    QUALITY_RECORDED,
    Station,
    Train,
    TrainStationDelay,
    Trip,
    TripStop,
)

STATION_ID = 1
TRAIN_ID = 1


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Station(id=STATION_ID, natural_key="bochum@51.478,7.223", name="Bochum Hbf",
                      lat=51.478, lon=7.223))
        s.add(Train(id=TRAIN_ID, source=SOURCE_TT, identity_key="tts:RE/10422",
                    category="RE", segment="RV", display_name="RE 10422"))
        s.flush()
        yield s


NOW = int(time.time())


def add_run(session, day: int, *, source=SOURCE_TT, quality=QUALITY_RECORDED,
            dep_delay=60, arr_delay=60, is_handover=0, observed_at=NOW):
    """One trip with a single stop at the fixture station.

    `day` counts weeks, not days: averages are grouped per day of the week, so
    runs meant to average together have to fall on the same weekday. 1 -> Tue
    2026-09-01, 2 -> Tue 2026-09-08, and so on. Tests that want two *different*
    weekdays call add_run_on_date directly.
    """
    return add_run_on_date(
        session, f"2026-09-{1 + 7 * (day - 1):02d}", key=str(day), source=source,
        quality=quality, dep_delay=dep_delay, arr_delay=arr_delay,
        is_handover=is_handover, observed_at=observed_at,
    )


def add_run_on_date(session, service_date: str, *, key: str | None = None,
                    source=SOURCE_TT, quality=QUALITY_RECORDED,
                    dep_delay=60, arr_delay=60, is_handover=0, observed_at=NOW):
    """One trip with a single stop at the fixture station, on an exact date."""
    key = key or service_date
    trip = Trip(train_id=TRAIN_ID, service_date=service_date, source=source,
                source_run_key=f"{source}:{key}", status="COMPLETED")
    session.add(trip)
    session.flush()
    session.add(TripStop(
        trip_id=trip.id, stop_sequence=1, station_id=STATION_ID,
        arr_delay_s=arr_delay, dep_delay_s=dep_delay, quality=quality,
        rule_version=1, is_handover=is_handover, last_observed_at=observed_at,
    ))
    session.flush()
    return trip


def averages(session) -> TrainStationDelay | None:
    run_delay_averages(session, full=True)
    return session.scalar(select(TrainStationDelay))


def test_mean_over_recorded_stops(session):
    for day, delay in enumerate([60, 120, 180], start=1):
        add_run(session, day, dep_delay=delay, arr_delay=delay)
    row = averages(session)
    assert row.n_dep == 3
    assert row.n_runs == 3
    assert row.avg_dep_delay_s == pytest.approx(120.0)
    assert row.segment == "RV"


def test_untrusted_grades_are_not_counted_as_on_time(session):
    """NO_DATA and PREDICTION_ONLY are unknowns, and must not enter the mean."""
    add_run(session, 1, dep_delay=600, arr_delay=600)
    add_run(session, 2, quality=QUALITY_NO_DATA, dep_delay=0, arr_delay=0)
    add_run(session, 3, quality=QUALITY_PREDICTION_ONLY, dep_delay=0, arr_delay=0)
    row = averages(session)
    assert row.n_dep == 1, "only the RECORDED stop is a sample"
    assert row.avg_dep_delay_s == pytest.approx(600.0), "absence must not dilute the mean"


def test_cancellations_are_counted_but_never_averaged(session):
    add_run(session, 1, dep_delay=300, arr_delay=300)
    add_run(session, 2, quality=QUALITY_CANCELLED, dep_delay=None, arr_delay=None)
    row = averages(session)
    assert row.n_dep == 1
    assert row.n_cancelled == 1
    assert row.n_known == 2
    assert row.avg_dep_delay_s == pytest.approx(300.0), "a cancellation is not a zero delay"


def test_handover_stops_are_excluded(session):
    """A run that changes number shows one physical stop on both sides."""
    add_run(session, 1, dep_delay=60, arr_delay=60)
    add_run(session, 2, dep_delay=6000, arr_delay=6000, is_handover=1)
    row = averages(session)
    assert row.n_dep == 1
    assert row.avg_dep_delay_s == pytest.approx(60.0)


def test_punctuality_uses_the_six_minute_threshold(session):
    # 359s is on time, 361s is not.
    for day, delay in enumerate([359, 361, 361, 361], start=1):
        add_run(session, day, dep_delay=delay, arr_delay=delay)
    row = averages(session)
    assert row.pct_dep_punctual == pytest.approx(0.25)


def test_each_weekday_gets_its_own_row(session):
    """Tuesday and Wednesday are different services, so different averages."""
    add_run_on_date(session, "2026-09-01", dep_delay=60, arr_delay=60)    # Tuesday
    add_run_on_date(session, "2026-09-08", dep_delay=120, arr_delay=120)  # Tuesday
    add_run_on_date(session, "2026-09-02", dep_delay=600, arr_delay=600)  # Wednesday

    run_delay_averages(session, full=True)
    rows = {row.dow: row for row in session.scalars(select(TrainStationDelay))}
    assert sorted(rows) == [1, 2], "one row per weekday seen, Monday=0"
    assert rows[1].n_dep == 2
    assert rows[1].avg_dep_delay_s == pytest.approx(90.0), "the two Tuesdays only"
    assert rows[2].n_dep == 1
    assert rows[2].avg_dep_delay_s == pytest.approx(600.0), "Wednesday is untouched by it"


def test_dow_matches_pythons_weekday_not_sqlites(session):
    """strftime('%w') is 0=Sunday; the dow column is 0=Monday. Pin the difference."""
    add_run_on_date(session, "2026-09-06")  # a Sunday
    run_delay_averages(session, full=True)
    row = session.scalar(select(TrainStationDelay))
    assert row.dow == 6, "Sunday is 6 here, even though SQLite would call it 0"


def test_a_weekday_that_loses_its_samples_loses_only_its_own_row(session):
    add_run_on_date(session, "2026-09-01", dep_delay=60, arr_delay=60)    # Tuesday
    add_run_on_date(session, "2026-09-02", dep_delay=600, arr_delay=600)  # Wednesday
    run_delay_averages(session, full=True)
    assert session.scalar(select(func.count()).select_from(TrainStationDelay)) == 2

    # Ungrade only the Wednesday stop, leaving a dirty marker behind.
    wednesday = session.scalar(select(Trip).where(Trip.service_date == "2026-09-02"))
    session.execute(
        TripStop.__table__.update()
        .where(TripStop.trip_id == wednesday.id)
        .values(quality=QUALITY_NO_DATA, last_observed_at=NOW + 1)
    )
    session.flush()

    run_delay_averages(session, full=False)
    rows = list(session.scalars(select(TrainStationDelay)))
    assert [r.dow for r in rows] == [1], "Tuesday survives; only Wednesday is dropped"


def test_full_pass_reconciles_pairs_whose_samples_vanished(session):
    """Wholesale deletion leaves nothing to mark a pair dirty, so --full owns it."""
    add_run(session, 1, dep_delay=60, arr_delay=60)
    assert averages(session) is not None

    session.execute(TripStop.__table__.delete())
    session.flush()

    run_delay_averages(session, full=False)
    assert session.scalar(select(TrainStationDelay)) is not None, (
        "the incremental pass has no signal that these samples ever existed"
    )

    run_delay_averages(session, full=True)
    assert session.scalar(select(TrainStationDelay)) is None, (
        "an average must not outlive the samples behind it"
    )


def test_incremental_pass_drops_a_pair_that_lost_its_last_good_sample(session):
    """Here a dirty marker survives, so the incremental pass can see the loss."""
    add_run(session, 1, dep_delay=60, arr_delay=60)
    assert averages(session) is not None

    # The stop is still there and still recently observed, but now ungraded.
    session.execute(
        TripStop.__table__.update().values(quality=QUALITY_NO_DATA, last_observed_at=NOW + 1)
    )
    session.flush()

    run_delay_averages(session, full=False)
    assert session.scalar(select(TrainStationDelay)) is None


@pytest.mark.parametrize(
    "values,fraction,expected",
    [
        ([10], 0.5, 10),
        ([10, 20, 30], 0.5, 20),
        ([10, 20, 30, 40], 0.9, 40),
        (list(range(1, 101)), 0.9, 90),
        ([], 0.5, None),
    ],
)
def test_percentile_is_nearest_rank(values, fraction, expected):
    assert _percentile(values, fraction) == expected
