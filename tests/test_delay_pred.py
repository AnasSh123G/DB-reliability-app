"""dep_pred/arr_pred/pred, sourced from stop_delay_average.

The stored average keys BOTH sides of a stop under ONE row -- at the
departure's local time if there is one, else the arrival's (derive/
stop_averages.py: COALESCE(supposed_departure_utc, supposed_arrival_utc)) --
so a through stop's arr_pred and dep_pred must be looked up at the SAME
sched_time, reading different columns of that one row. This was wrong in an
earlier version of queries.py (each side used its own raw time as the lookup
key), which silently returned None for arr_pred on every through stop with
a real average on file. These tests pin the fix.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from datetime import date

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.ingest.timeutil import service_date_anchor
from dbrel.models import (
    ServiceLine,
    Station,
    StationStop,
    StopDelayAverage,
    Train,
)
from dbrel.ingest.runs import tt_feed_version
from dbrel.web import queries

LINE_ID = 9
TRAIN_ID = 700
ORIGIN_DEP_SEC = 9 * 3600  # 09:00
THROUGH_ARR_SEC = 9 * 3600 + 2160  # 09:36
THROUGH_DEP_SEC = 9 * 3600 + 2280  # 09:38
ROUTE = ["Aachen Hbf", "Duisburg Hbf"]


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        fv_id = tt_feed_version(s)
        s.add(
            ServiceLine(
                id=LINE_ID, identity_key="NX/RE1", category="NX", line="RE1",
                display_name="RE1", search_name="re1", operator="NXRE",
            )
        )
        s.add(
            Station(
                id=3, natural_key="duisburg@51.4,6.8", name="Duisburg Hbf",
                lat=51.4, lon=6.8, tracked=1, eva=8000086,
            )
        )
        s.add(
            Train(
                id=TRAIN_ID, source=SOURCE_TT, identity_key="tts:NX/26811",
                category="NX", segment="RV", train_number=26811, operator="NXRE",
                display_name="NX 26811", line_id=LINE_ID,
                origin_name="Aachen Hbf", dest_name="Duisburg Hbf",
                first_departure_sec=ORIGIN_DEP_SEC, n_stops=2,
                route_path="|".join(ROUTE),
            )
        )
        s.flush()
        s.add(
            StationStop(
                feed_version_id=fv_id, train_id=TRAIN_ID, stop_sequence=2,
                station_id=3, planned_arrival_sec=THROUGH_ARR_SEC,
                planned_departure_sec=THROUGH_DEP_SEC, platform_code="12",
            )
        )
        # Both sides live under ONE row, keyed at the departure's time
        # (09:38) since a departure exists -- matching derive/stop_averages.py's
        # COALESCE. n_arr is deliberately 0 with no mean, independent of a
        # populated n_dep -- see stop-delay-averages spec's per-side rule.
        s.add(
            StopDelayAverage(
                station_id=3, line_id=LINE_ID, direction=0,
                trip_start_time="09:00", sched_time="09:38", is_holiday=0,
                n_arr=0, mean_arr_delay_s=None,
                n_dep=10, mean_dep_delay_s=180.0,
                built_at=0,
            )
        )
        s.flush()
        yield s


def _stop(session, name):
    return next(s for s in queries.train_route(session, TRAIN_ID)["stops"] if s["name"] == name)


def test_dep_pred_uses_the_coalesced_sched_time(session):
    stop = _stop(session, "Duisburg Hbf")
    assert stop["dep_pred"] == {"p50_min": 3, "lo_min": 3, "hi_min": 3}


def test_arr_pred_looks_up_the_same_row_as_dep_pred(session):
    """Not the arrival's own raw time (09:36) -- the row lives at 09:38."""
    stop = _stop(session, "Duisburg Hbf")
    assert stop["arr_pred"] is None  # n_arr is 0 on that row


def test_arr_pred_populates_once_the_row_has_arrival_samples(session):
    row = session.get(StopDelayAverage, (3, LINE_ID, 0, "09:00", "09:38", 0))
    row.n_arr = 5
    row.mean_arr_delay_s = 60.0
    session.flush()
    stop = _stop(session, "Duisburg Hbf")
    assert stop["arr_pred"] == {"p50_min": 1, "lo_min": 1, "hi_min": 1}
    assert stop["dep_pred"] == {"p50_min": 3, "lo_min": 3, "hi_min": 3}


def test_station_board_and_trips_around_agree_with_train_route(session):
    today = date.today()
    now_epoch = service_date_anchor(today) + THROUGH_DEP_SEC
    board_calls = queries.station_board(session, 3, now=now_epoch)["calls"]
    call = next(c for c in board_calls if c["train_id"] == TRAIN_ID)
    assert call["dep_pred"] == {"p50_min": 3, "lo_min": 3, "hi_min": 3}

    around = queries.trips_around(session, LINE_ID, 0, 3, THROUGH_DEP_SEC)
    trip = next(t for t in around["trips"] if t["train_id"] == TRAIN_ID)
    assert trip["dep_pred"] == {"p50_min": 3, "lo_min": 3, "hi_min": 3}


def test_line_detail_run_pred_is_the_origin_departure_average(session):
    origin_row = StopDelayAverage(
        station_id=None, line_id=LINE_ID, direction=0,
        trip_start_time="09:00", sched_time="09:00", is_holiday=0,
        n_arr=0, mean_arr_delay_s=None, n_dep=4, mean_dep_delay_s=120.0,
        built_at=0,
    )
    # The origin station (Aachen Hbf, id=1 by dbrel convention elsewhere) --
    # give it a real id here so the row is reachable.
    aachen = Station(
        id=1, natural_key="aachen@50.7,6.1", name="Aachen Hbf",
        lat=50.7, lon=6.1, tracked=1, eva=8000001,
    )
    session.add(aachen)
    session.flush()
    fv_id = session.query(StationStop.feed_version_id).first()[0]
    session.add(
        StationStop(
            feed_version_id=fv_id, train_id=TRAIN_ID, stop_sequence=1,
            station_id=1, planned_arrival_sec=None,
            planned_departure_sec=ORIGIN_DEP_SEC, platform_code="1",
        )
    )
    origin_row.station_id = 1
    session.add(origin_row)
    session.flush()

    detail = queries.line_detail(session, LINE_ID)
    run = next(r for d in detail["directions"] for r in d["runs"] if r["id"] == TRAIN_ID)
    assert run["pred"] == {"p50_min": 2, "lo_min": 2, "hi_min": 2}
