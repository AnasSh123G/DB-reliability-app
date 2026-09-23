"""stop_delay_average build: key separation, quality filtering, determinism."""

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
STATION_A, STATION_B, STATION_C = 1, 2, 3


def _epoch(y, m, d, h, mi):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


@pytest.fixture
def engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(ServiceLine(id=LINE_ID, identity_key="RE/1", category="RE", line="RE1",
                          display_name="RE1", search_name="re1"))
        s.add(Station(id=STATION_A, natural_key="a", name="A", lat=1, lon=1, state="DE-NW"))
        s.add(Station(id=STATION_B, natural_key="b", name="B", lat=2, lon=2, state="DE-NW"))
        s.add(Station(id=STATION_C, natural_key="c", name="C", lat=3, lon=3, state="DE-NW"))

        # Direction 0: A -> B -> C, two distinct daily departures (06:xx, 12:xx).
        s.add(Train(id=10, identity_key="tr10", line_id=LINE_ID, route_path="A|B|C",
                    origin_name="A", dest_name="C", first_departure_sec=6 * 3600))
        s.add(Train(id=11, identity_key="tr11", line_id=LINE_ID, route_path="A|B|C",
                    origin_name="A", dest_name="C", first_departure_sec=12 * 3600))
        # Direction 1: C -> B -> A.
        s.add(Train(id=20, identity_key="tr20", line_id=LINE_ID, route_path="C|B|A",
                    origin_name="C", dest_name="A", first_departure_sec=8 * 3600))
        s.flush()

        trip_id = 0
        ts_id = 0

        def add_trip_stop(train_id, service_date, station_id, arr_delay, dep_delay,
                          *, cancelled=0, quality="RECORDED", is_holiday=0,
                          dep_hhmm=(6, 10), arr_hhmm=(6, 9)):
            nonlocal trip_id, ts_id
            trip_id += 1
            ts_id += 1
            y, m, d = (int(x) for x in service_date.split("-"))
            s.add(Trip(id=trip_id, train_id=train_id, service_date=service_date,
                       source_run_key=f"r{trip_id}"))
            s.add(TripStop(
                id=ts_id, trip_id=trip_id, stop_sequence=1, station_id=station_id,
                supposed_arrival_utc=_epoch(y, m, d, *arr_hhmm),
                supposed_departure_utc=_epoch(y, m, d, *dep_hhmm),
                arr_delay_s=arr_delay, dep_delay_s=dep_delay,
                quality=quality, rule_version=1, cancelled=cancelled, is_holiday=is_holiday,
            ))
            return ts_id

        # Direction 0, 06:10 service, station B, two dates -> one averaged row.
        add_trip_stop(10, "2026-09-15", STATION_B, 60, 90, dep_hhmm=(6, 10), arr_hhmm=(6, 9))
        add_trip_stop(10, "2026-09-22", STATION_B, 100, 110, dep_hhmm=(6, 10), arr_hhmm=(6, 9))

        # Direction 0, 12:10 service, station B -> must stay a separate row.
        add_trip_stop(11, "2026-09-15", STATION_B, 20, 30, dep_hhmm=(12, 10), arr_hhmm=(12, 9))

        # Direction 1, station B -> must stay separate from direction 0.
        add_trip_stop(20, "2026-09-15", STATION_B, 200, 210, dep_hhmm=(8, 15), arr_hhmm=(8, 14))

        # Cancelled -- must not affect any mean/count.
        add_trip_stop(10, "2026-09-16", STATION_B, 9999, 9999, dep_hhmm=(6, 10), arr_hhmm=(6, 9),
                      cancelled=1)

        # Non-RECORDED quality -- must not affect any mean/count.
        add_trip_stop(10, "2026-09-17", STATION_B, 9999, 9999, dep_hhmm=(6, 10), arr_hhmm=(6, 9),
                      quality="NO_DATA")

        # is_holiday NULL (unresolved) -- must be excluded entirely.
        add_trip_stop(10, "2026-09-18", STATION_B, 9999, 9999, dep_hhmm=(6, 10), arr_hhmm=(6, 9),
                      is_holiday=None)

        s.commit()
    return engine


def _rows(engine):
    with Session(engine, future=True) as s:
        return s.execute(select(StopDelayAverage)).scalars().all()


def test_two_directions_stay_separate(engine):
    with Session(engine, future=True) as s:
        build_stop_averages(engine, s)
        s.commit()
    rows = [r for r in _rows(engine) if r.station_id == STATION_B]
    directions = {r.direction for r in rows}
    assert directions == {0, 1}


def test_two_distinct_daily_departures_stay_separate(engine):
    with Session(engine, future=True) as s:
        build_stop_averages(engine, s)
        s.commit()
    rows = [r for r in _rows(engine) if r.station_id == STATION_B and r.direction == 0]
    sched_times = {r.sched_time for r in rows}
    assert sched_times == {"06:10", "12:10"}


def test_mean_pools_across_dates_at_the_same_key(engine):
    with Session(engine, future=True) as s:
        build_stop_averages(engine, s)
        s.commit()
    row = next(
        r for r in _rows(engine)
        if r.station_id == STATION_B and r.direction == 0 and r.sched_time == "06:10"
    )
    assert row.n_arr == 2
    assert row.n_dep == 2
    assert row.mean_arr_delay_s == pytest.approx((60 + 100) / 2)
    assert row.mean_dep_delay_s == pytest.approx((90 + 110) / 2)


def test_cancelled_and_non_recorded_and_unlabelled_rows_are_excluded(engine):
    with Session(engine, future=True) as s:
        build_stop_averages(engine, s)
        s.commit()
    row = next(
        r for r in _rows(engine)
        if r.station_id == STATION_B and r.direction == 0 and r.sched_time == "06:10"
    )
    # Only the two legitimate RECORDED, non-cancelled, labelled rows count --
    # the cancelled, NO_DATA, and is_holiday=NULL rows added at 06:10 must not
    # inflate the sample count or drag the mean towards their sentinel 9999s.
    assert row.n_arr == 2
    assert row.mean_arr_delay_s < 1000


def test_two_trips_with_different_start_times_stay_separate_even_at_the_same_sched_time(engine):
    """The coupled-portions case: two different trains, same line, same
    direction, reaching the shared station at the same scheduled clock time,
    but with different origin start times (encoded here via each train's own
    `first_departure_sec`, the GTFS-fallback path for a 'gtfs'-source trip --
    see derive/stop_averages.py's docstring) -- must not be pooled together.
    """
    with Session(engine, future=True) as s:
        s.add(Train(id=30, identity_key="tr30", line_id=LINE_ID, route_path="A|B|C",
                    origin_name="A", dest_name="C", first_departure_sec=7 * 3600))
        s.add(Train(id=31, identity_key="tr31", line_id=LINE_ID, route_path="A|B|C",
                    origin_name="A", dest_name="C", first_departure_sec=7 * 3600 + 15 * 60))
        s.flush()
        for trip_id, train_id, delay in ((101, 30, 40), (102, 31, 80)):
            s.add(Trip(id=trip_id, train_id=train_id, service_date="2026-09-15",
                       source_run_key=f"cp{trip_id}"))
            s.add(TripStop(
                id=trip_id, trip_id=trip_id, stop_sequence=1, station_id=STATION_B,
                supposed_arrival_utc=_epoch(2026, 9, 15, 9, 29),
                supposed_departure_utc=_epoch(2026, 9, 15, 9, 30),
                arr_delay_s=delay, dep_delay_s=delay,
                quality="RECORDED", rule_version=1, cancelled=0, is_holiday=0,
            ))
        s.commit()

    with Session(engine, future=True) as s:
        build_stop_averages(engine, s)
        s.commit()

    rows = [
        r for r in _rows(engine)
        if r.station_id == STATION_B and r.direction == 0 and r.sched_time == "09:30"
        and r.is_holiday == 0
    ]
    starts = {r.trip_start_time for r in rows}
    assert starts == {"07:00", "07:15"}
    assert len(rows) == 2
    for r in rows:
        assert r.n_arr == 1 and r.n_dep == 1


def test_implausible_delay_excluded_per_side_independently(engine):
    """A stop with an implausible arrival delay (> MAX_PLAUSIBLE_DELAY_S) but a
    plausible departure delay must still contribute to n_dep/mean_dep_delay_s
    -- the cap applies per side, not per row. See derive/stop_averages.py's
    MAX_PLAUSIBLE_DELAY_S (S9/S46 data-quality finding).
    """
    with Session(engine, future=True) as s:
        s.add(Train(id=40, identity_key="tr40", line_id=LINE_ID, route_path="A|B|C",
                    origin_name="A", dest_name="C", first_departure_sec=14 * 3600))
        s.flush()
        s.add(Trip(id=200, train_id=40, service_date="2026-09-15", source_run_key="r200"))
        s.add(TripStop(
            id=200, trip_id=200, stop_sequence=1, station_id=STATION_B,
            supposed_arrival_utc=_epoch(2026, 9, 15, 14, 9),
            supposed_departure_utc=_epoch(2026, 9, 15, 14, 10),
            arr_delay_s=10000, dep_delay_s=100,
            quality="RECORDED", rule_version=1, cancelled=0, is_holiday=0,
        ))
        s.commit()

    with Session(engine, future=True) as s:
        build_stop_averages(engine, s)
        s.commit()

    row = next(
        r for r in _rows(engine)
        if r.station_id == STATION_B and r.direction == 0 and r.sched_time == "14:10"
    )
    assert row.n_dep == 1
    assert row.mean_dep_delay_s == pytest.approx(100)
    assert row.n_arr == 0
    assert row.mean_arr_delay_s is None


def test_build_is_deterministic(engine):
    with Session(engine, future=True) as s:
        build_stop_averages(engine, s)
        s.commit()
    first = {
        (r.station_id, r.line_id, r.direction, r.sched_time, r.is_holiday):
        (r.n_arr, r.mean_arr_delay_s, r.n_dep, r.mean_dep_delay_s)
        for r in _rows(engine)
    }
    with Session(engine, future=True) as s:
        build_stop_averages(engine, s)
        s.commit()
    second = {
        (r.station_id, r.line_id, r.direction, r.sched_time, r.is_holiday):
        (r.n_arr, r.mean_arr_delay_s, r.n_dep, r.mean_dep_delay_s)
        for r in _rows(engine)
    }
    assert first == second


def test_line_less_train_is_averaged_per_train_not_per_line(engine):
    """ICE/IC-style trains have no line: they land in train_stop_delay_average."""
    from dbrel.models import TrainStopDelayAverage

    with Session(engine, future=True) as s:
        s.add(Train(id=50, identity_key="ice50", line_id=None, route_path="A|B|C",
                    origin_name="A", dest_name="C", first_departure_sec=7 * 3600))
        s.flush()
        for n, (date, dep_delay) in enumerate((("2026-09-15", 120), ("2026-09-22", 240))):
            s.add(Trip(id=900 + n, train_id=50, service_date=date, source_run_key=f"ice{n}"))
            y, m, d = (int(x) for x in date.split("-"))
            s.add(TripStop(
                id=900 + n, trip_id=900 + n, stop_sequence=2, station_id=STATION_B,
                supposed_arrival_utc=_epoch(y, m, d, 7, 29),
                supposed_departure_utc=_epoch(y, m, d, 7, 30),
                arr_delay_s=60, dep_delay_s=dep_delay,
                quality="RECORDED", rule_version=1, cancelled=0, is_holiday=0,
            ))
        s.commit()
        report = build_stop_averages(engine, s)

    with Session(engine, future=True) as s:
        rows = s.execute(select(TrainStopDelayAverage)).scalars().all()
    assert report["train_rows"] == 1
    (row,) = rows
    assert (row.train_id, row.station_id, row.trip_start_time, row.sched_time) == (50, STATION_B, "07:00", "07:30")
    assert row.n_dep == 2 and row.mean_dep_delay_s == 180
    # ...and never in the per-line table, which it has no key for.
    assert all(r.line_id == LINE_ID for r in _rows(engine))
