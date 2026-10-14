"""Integration contracts for actual observations in the autumn DST fold."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.config import TZ
from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.ingest.changes_poller import run_tt_poll
from dbrel.ingest.client import Response
from dbrel.ingest.parser import parse_ts
from dbrel.ingest.plan_import import import_plan
from dbrel.models import Station, Trip, TripStop

EVA_A, EVA_B = 8_020_101, 8_020_102
DST_DAY, DST_STAMP = "2026-10-25", "2610250250"
NORMAL_DAY, NORMAL_STAMP = "2026-10-26", "2610260250"


@pytest.fixture
def session():
    engine = __import__("sqlalchemy").create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        stations = [
            Station(natural_key="fold-a@1,1", name="Alpha", lat=1, lon=1, eva=EVA_A, tracked=1),
            Station(natural_key="fold-b@2,2", name="Beta", lat=2, lon=2, eva=EVA_B, tracked=1),
        ]
        db.add_all(stations)
        db.flush()
        yield db, stations
    engine.dispose()


def _epoch(day: str, hour: int, minute: int, *, fold=0):
    year, month, day_num = map(int, day.split("-"))
    return int(datetime(year, month, day_num, hour, minute, tzinfo=TZ, fold=fold).timestamp())


def _plan_body(eva: int, stamp: str, seq: int, *, arr: str, dep: str,
               before: str = "", after: str = "") -> bytes:
    return (f'<timetable eva="{eva}"><s id="123456789-{stamp}-{seq}" eva="{eva}">'
            '<tl f="N" t="p" o="OP" c="RE" n="42"/>'
            f'<ar pt="{arr}" pp="1" ppth="{before}"/>'
            f'<dp pt="{dep}" pp="1" ppth="{after}"/>'
            '</s></timetable>').encode()


def _changes_body(eva: int, stamp: str, seq: int, *, arr: str | None = None,
                  dep: str | None = None, cancel_arr=False) -> bytes:
    arrival = ""
    if arr is not None or cancel_arr:
        attrs = ('cs="c" clt="2610250200"' if cancel_arr
                 else f'ct="{arr}" l="RE42"')
        arrival = f"<ar {attrs}/>"
    departure = f'<dp ct="{dep}" l="RE42"/>' if dep is not None else ""
    return (f'<timetable eva="{eva}"><s id="123456789-{stamp}-{seq}" eva="{eva}">'
            f'{arrival}{departure}</s></timetable>').encode()


class _FixtureClient:
    def __init__(self, plans, changes, recorded_at):
        self.plans, self.change_bodies, self.recorded_at = plans, changes, recorded_at

    def plan(self, eva, _date, _hour):
        return Response(status=200, body=self.plans[eva], elapsed_ms=0, recorded_at=self.recorded_at)

    def changes(self, eva):
        return Response(status=200, body=self.change_bodies[eva], elapsed_ms=0,
                        recorded_at=self.recorded_at)


def _add_run(session, stations, day, stamp, *, actuals, observed_at):
    day_code = stamp[:6]
    dest_arr = f"{day_code}0210" if day == DST_DAY else f"{day_code}0310"
    dest_dep = f"{day_code}0300" if day == DST_DAY else f"{day_code}0320"
    plan_a = _plan_body(EVA_A, stamp, 1, arr=f"{day_code}0250", dep=f"{day_code}0255",
                        after="Beta")
    plan_b = _plan_body(EVA_B, stamp, 2, arr=dest_arr, dep=dest_dep,
                        before="Alpha")
    plans = {EVA_A: plan_a, EVA_B: plan_b}
    trip_id = f"123456789-{stamp}"
    changes = {
        EVA_A: _changes_body(EVA_A, stamp, 1, arr=actuals.get("origin_arr"),
                             dep=actuals.get("origin_dep")),
        EVA_B: _changes_body(EVA_B, stamp, 2, arr=actuals.get("dest_arr"),
                             dep=actuals.get("dest_dep"), cancel_arr=actuals.get("cancel_dest_arr", False)),
    }
    import_plan(session, _FixtureClient(plans, {}, observed_at), date=day, hours=[2],
                force=True, workers=1, now=observed_at)
    trip = session.scalar(select(Trip).where(
        Trip.source == SOURCE_TT, Trip.source_run_key == trip_id, Trip.service_date == day,
    ))
    assert trip is not None
    poll = run_tt_poll(session, _FixtureClient({}, changes, observed_at), priority_max=99,
                       now=observed_at)
    return trip, poll


def _fact(session, trip, seq):
    return session.scalar(select(TripStop).where(
        TripStop.trip_id == trip.id, TripStop.stop_sequence == seq,
    ))


def test_two_station_fold_abstains_ambiguous_arrivals_but_keeps_clear_departure(session):
    db, stations = session
    observed = _epoch(DST_DAY, 4, 0)
    trip, _ = _add_run(db, stations, DST_DAY, DST_STAMP,
                       actuals={"origin_arr": DST_STAMP, "origin_dep": "2610250255",
                                "dest_arr": "2610250220", "dest_dep": "2610250320"},
                       observed_at=observed)
    destination = _fact(db, trip, 2)
    assert destination is not None
    assert destination.actual_arrival_utc is None
    assert destination.arr_delay_s is None
    assert destination.actual_departure_utc == parse_ts("2610250320")
    assert destination.dep_delay_s == 20 * 60
    origin = _fact(db, trip, 1)
    assert origin is None or origin.actual_arrival_utc is None


def test_non_dst_actual_arrival_is_still_recorded(session):
    db, stations = session
    observed = _epoch(NORMAL_DAY, 4, 0)
    trip, _ = _add_run(db, stations, NORMAL_DAY, NORMAL_STAMP,
                       actuals={"origin_arr": "2610260255", "dest_arr": "2610260320",
                                "dest_dep": "2610260325"}, observed_at=observed)
    destination = _fact(db, trip, 2)
    assert destination is not None
    assert destination.actual_arrival_utc == parse_ts("2610260320")
    assert destination.arr_delay_s == 10 * 60
    assert destination.actual_departure_utc == parse_ts("2610260325")


def test_fold_cancellation_is_retained_without_fabricating_actual_time(session):
    db, stations = session
    observed = _epoch(DST_DAY, 4, 0)
    trip, poll = _add_run(db, stations, DST_DAY, DST_STAMP,
                          actuals={"cancel_dest_arr": True}, observed_at=observed)
    destination = _fact(db, trip, 2)
    assert poll.cancellations == 1
    assert destination is not None
    assert destination.arr_cancelled == 1
    assert destination.actual_arrival_utc is None
    assert destination.arr_delay_s is None


def test_second_fold_event_is_not_actual_before_its_planned_instant(session):
    db, stations = session
    first_fold_now = _epoch(DST_DAY, 2, 45, fold=0)
    trip, _ = _add_run(db, stations, DST_DAY, DST_STAMP,
                       actuals={"dest_arr": "2610250220"}, observed_at=first_fold_now)
    destination = _fact(db, trip, 2)
    assert destination is None or destination.actual_arrival_utc is None
