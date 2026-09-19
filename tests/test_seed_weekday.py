"""The weekday is a first-class dimension of the seeded data.

The seven day_* views and the dow column on train_station_delay exist to separate
weekdays, and they are only worth anything if the weekdays actually differ. These
tests pin the two ways they differ -- how much runs, and how late it is -- plus the
spread across trains and dates that keeps a per-train average informative.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    QUALITY_RECORDED,
    Station,
    StationStop,
    Train,
    TrainStationDelay,
    Trip,
    TripStop,
)
from dbrel.seed import DOW_DELAY_SCALE, DOW_SERVICE_RATE, date_shocks, seed_synthetic
from dbrel.timetables.runs import tt_feed_version

MONDAY, TUESDAY, FRIDAY, SATURDAY, SUNDAY = 0, 1, 4, 5, 6

N_TRAINS = 40
# Long enough that a per-weekday mean is not noise: 28 weeks is 28 of each weekday.
DAYS = 196


@pytest.fixture(scope="module")
def session():
    """Forty RV trains, each calling at both stations, over 28 full weeks."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        fv_id = tt_feed_version(s)
        s.add(
            Station(
                id=1,
                natural_key="bochum@51.478,7.223",
                name="Bochum Hbf",
                lat=51.478,
                lon=7.223,
                tracked=1,
            )
        )
        s.add(
            Station(
                id=2,
                natural_key="essen@51.451,7.014",
                name="Essen Hbf",
                lat=51.451,
                lon=7.014,
                tracked=1,
            )
        )
        for train_id in range(1, N_TRAINS + 1):
            s.add(
                Train(
                    id=train_id,
                    source=SOURCE_TT,
                    identity_key=f"tts:RE/{train_id}",
                    category="RE",
                    segment="RV",
                )
            )
        s.flush()
        for train_id in range(1, N_TRAINS + 1):
            base = 6 * 3600 + train_id * 120
            s.add(
                StationStop(
                    feed_version_id=fv_id,
                    train_id=train_id,
                    stop_sequence=1,
                    station_id=1,
                    planned_arrival_sec=base,
                    planned_departure_sec=base + 60,
                )
            )
            s.add(
                StationStop(
                    feed_version_id=fv_id,
                    train_id=train_id,
                    stop_sequence=2,
                    station_id=2,
                    planned_arrival_sec=base + 1200,
                    planned_departure_sec=base + 1260,
                )
            )
        s.flush()
        seed_synthetic(s, days=DAYS, seed=99)
        yield s


def _by_weekday(session) -> dict[int, list[int]]:
    rows = session.execute(
        select(Trip.service_date, TripStop.dep_delay_s)
        .join(TripStop, TripStop.trip_id == Trip.id)
        .where(TripStop.quality == QUALITY_RECORDED, TripStop.dep_delay_s.isnot(None))
    ).all()
    out: dict[int, list[int]] = {}
    for service_date, delay in rows:
        out.setdefault(date.fromisoformat(service_date).weekday(), []).append(delay)
    return out


def _trips_on(session, dow: int) -> int:
    dates = session.execute(select(Trip.service_date).distinct()).scalars().all()
    wanted = [d for d in dates if date.fromisoformat(d).weekday() == dow]
    return session.scalar(
        select(func.count()).select_from(Trip).where(Trip.service_date.in_(wanted))
    )


def test_friday_is_the_calibration_anchor():
    """The profiles are fitted to a real Friday, so Friday must not be rescaled.

    If this ever changes, the measured-Friday targets asserted in test_seed.py stop
    describing the Fridays the seeder actually writes, and the whole calibration
    becomes unfalsifiable.
    """
    assert DOW_DELAY_SCALE[FRIDAY] == 1.0
    assert DOW_SERVICE_RATE[FRIDAY] == 1.0


@pytest.mark.parametrize("days", [28, 56, 196])
def test_date_shocks_cannot_bias_a_weekday(days):
    """The date shock must not leak into the weekday means.

    Regression: on the first real 28-day run the four Sundays drew shocks
    averaging +51 s against the Saturdays' -11.5 s, which pushed Sunday above
    Saturday and inverted the ordering DOW_DELAY_SCALE sets. A month holds only
    four of each weekday, so at sigma=45 this is ordinary luck, not bad luck --
    it has to be removed by construction rather than hoped away.
    """
    first = date(2026, 8, 20)
    dates = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    shocks = date_shocks(20260914, dates)

    by_dow: dict[int, list[int]] = {}
    for service_date, value in shocks.items():
        by_dow.setdefault(date.fromisoformat(service_date).weekday(), []).append(value)

    for dow, values in by_dow.items():
        assert abs(statistics.fmean(values)) <= 1.0, f"weekday {dow} carries a net shock"
    # ...while individual dates still differ, which is the shock's whole purpose.
    assert statistics.pstdev(list(shocks.values())) > 20


def test_weekends_run_a_thinner_timetable(session):
    weekday = _trips_on(session, TUESDAY)
    assert _trips_on(session, SATURDAY) < weekday
    assert _trips_on(session, SUNDAY) < _trips_on(session, SATURDAY)


def test_the_same_services_are_missing_every_sunday(session):
    """A timetable, not a fresh random subset each week.

    This is what fails if the service draw is taken from the run-level rng rather
    than deterministically from (train_id, weekday).
    """
    sundays = sorted(
        d
        for d in session.execute(select(Trip.service_date).distinct()).scalars().all()
        if date.fromisoformat(d).weekday() == SUNDAY
    )
    assert len(sundays) >= 2

    def trains_on(service_date: str) -> set[int]:
        return set(
            session.execute(
                select(Trip.train_id).where(Trip.service_date == service_date)
            ).scalars()
        )

    first = trains_on(sundays[0])
    assert first, "some trains must still run on a Sunday"
    for other in sundays[1:]:
        assert trains_on(other) == first


def test_weekday_delay_levels_differ(session):
    by_dow = _by_weekday(session)
    means = {dow: statistics.fmean(v) for dow, v in by_dow.items()}
    assert len(means) == 7

    # Weekends are quieter, so they are less late; the ordering is the one
    # DOW_DELAY_SCALE encodes rather than an accident of the draw.
    assert means[SUNDAY] < means[SATURDAY] < means[TUESDAY]
    assert means[MONDAY] > means[TUESDAY]


def test_trains_do_not_all_share_one_average(session):
    """Without a per-train bias every train at a station converges on one number."""
    rows = session.execute(
        select(TripStop.trip_id, TripStop.dep_delay_s, Trip.train_id)
        .join(Trip, Trip.id == TripStop.trip_id)
        .where(
            TripStop.station_id == 1,
            TripStop.quality == QUALITY_RECORDED,
            TripStop.dep_delay_s.isnot(None),
        )
    ).all()
    per_train: dict[int, list[int]] = {}
    for _, delay, train_id in rows:
        per_train.setdefault(train_id, []).append(delay)

    means = [statistics.fmean(v) for v in per_train.values() if len(v) > 50]
    assert len(means) > 10
    # _train_bias draws from gauss(0, 60); anything near zero spread means it is
    # not being applied.
    assert statistics.pstdev(means) > 20


def test_two_fridays_are_not_the_same_day_twice(session):
    """The per-date shock: four Fridays must differ, not repeat one distribution."""
    by_date: dict[str, list[int]] = {}
    rows = session.execute(
        select(Trip.service_date, TripStop.dep_delay_s)
        .join(TripStop, TripStop.trip_id == Trip.id)
        .where(TripStop.quality == QUALITY_RECORDED, TripStop.dep_delay_s.isnot(None))
    ).all()
    for service_date, delay in rows:
        if date.fromisoformat(service_date).weekday() == FRIDAY:
            by_date.setdefault(service_date, []).append(delay)

    means = [statistics.fmean(v) for v in by_date.values() if len(v) > 20]
    assert len(means) > 4
    assert statistics.pstdev(means) > 10, "every Friday came out the same"


def test_seeding_writes_no_averages(session):
    """The seeder produces facts only; averaging is a separate, explicit pass."""
    assert session.scalar(select(func.count()).select_from(TrainStationDelay)) == 0


def test_synthetic_dates_never_collide_with_a_weekday_they_are_not(session):
    """A row in day_sunday cannot carry a Friday's date.

    Trivially true given service_date is a real calendar date, and worth pinning:
    it is the property that makes the seven views a partition rather than a filter.
    """
    dates = session.execute(select(Trip.service_date).distinct()).scalars().all()
    assert len(dates) == len(set(dates))
    for service_date in dates:
        parsed = date.fromisoformat(service_date)
        assert parsed.isoformat() == service_date
    span = max(dates), min(dates)
    assert (date.fromisoformat(span[0]) - date.fromisoformat(span[1])) == timedelta(days=DAYS - 1)
