"""Regression coverage for dated IRIS schedule receipts.

The same recurring train can keep its train number, route, and stop index while
IRIS changes the published schedule from one service date to another.  The
historical plan for either date must remain available to poll and finalize that
date's facts.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dbrel.config import GRACE_S, TZ
from dbrel.db import Base
from dbrel.ingest.changes_poller import run_tt_poll
from dbrel.ingest.client import Response
from dbrel.ingest.finalize_tt import run_finalize_tt
from dbrel.ingest.plan_import import import_plan
from dbrel.models import QUALITY_RECORDED, Station, Trip, TripStop

EVA = 8000123
DAY1 = "2026-09-01"
DAY2 = "2026-09-02"
ID1 = "123456789-2609011200-1"
ID2 = "123456789-2609021100-1"


def _plan(day_stamp: str, clock: str = "1200") -> bytes:
    return (
        f'<timetable station="Example" eva="{EVA}">'
        f'<s id="123456789-{day_stamp}{clock}-1">'
        '<tl f="N" t="p" o="OP" c="RE" n="42"/>'
        f'<ar pt="{day_stamp}{clock}" pp="1" l="RE42" fb="RE42" ppth="North|South"/>'
        f'<dp pt="{day_stamp}{clock}" pp="1" l="RE42" fb="RE42" ppth="North|South"/>'
        '</s></timetable>'
    ).encode()


def _fchg(stop_id: str, actual: str, *, pt: bool = False) -> bytes:
    planned = f' pt="{actual[:6]}1200"' if pt else ""
    return (
        f'<timetable station="Example" eva="{EVA}">'
        f'<s id="{stop_id}" eva="{EVA}">'
        f'<ar{planned} ct="{actual}" l="RE42"/>'
        f'<dp{planned} ct="{actual}" l="RE42"/>'
        '</s></timetable>'
    ).encode()


class _Source:
    def __init__(self, plans: dict[str, bytes], changes: bytes | None = None):
        self.plans = plans
        self.changes_body = changes

    def plan(self, eva: int, date: str, hour: int) -> Response:
        # Import asks each station-hour bucket; fixtures use the same scheduled
        # event in the 12:00 bucket.
        body = self.plans.get(f"20{date[:2]}-{date[2:4]}-{date[4:6]}")
        return Response(status=200, body=body, elapsed_ms=0) if body else Response(
            status=404, body=b"", elapsed_ms=0
        )

    def changes(self, eva: int) -> Response:
        return Response(status=200, body=self.changes_body, elapsed_ms=0) if self.changes_body else Response(
            status=404, body=b"", elapsed_ms=0
        )


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Station(
            natural_key="example@1,1", name="Example", lat=1, lon=1,
            eva=EVA, tracked=1,
        ))
        db.commit()
        yield db
    engine.dispose()


def _epoch(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 9, day, hour, minute, tzinfo=TZ).timestamp())


def _import_dates(session: Session) -> None:
    source = _Source({DAY1: _plan("260901"), DAY2: _plan("260902", "1100")})
    import_plan(session, source, date=DAY1, hours=[12], force=True, now=_epoch(1, 12))
    import_plan(session, source, date=DAY2, hours=[11], force=True, now=_epoch(2, 11))
    session.flush()


def test_import_keeps_both_schedules_for_same_train_and_stop(session):
    _import_dates(session)
    trips = session.execute(select(Trip).order_by(Trip.service_date)).scalars().all()
    assert [trip.service_date for trip in trips] == [DAY1, DAY2]
    assert trips[0].train_id == trips[1].train_id
    assert trips[0].path_sha1 == trips[1].path_sha1
    assert trips[0].supposed_departure_utc == _epoch(1, 12)
    assert trips[1].supposed_departure_utc == _epoch(2, 11)


def test_fchg_delta_uses_exact_date_plan_and_retains_each_ten_minute_delay(session):
    _import_dates(session)
    first = _Source({}, _fchg(ID1, "2609011210"))
    second = _Source({}, _fchg(ID2, "2609021110"))
    # Each observation is interpreted against its date's schedule: 12:00 on
    # day one and 11:00 on day two. Both ran ten minutes late.
    run_tt_poll(session, first, priority_max=99, now=_epoch(1, 13) + GRACE_S + 1)
    run_tt_poll(session, second, priority_max=99, now=_epoch(2, 13) + GRACE_S + 1)
    session.flush()
    trips = session.execute(select(Trip).order_by(Trip.service_date)).scalars().all()
    facts = {
        trip.service_date: session.execute(
            select(TripStop).where(TripStop.trip_id == trip.id)
        ).scalars().one()
        for trip in trips
    }
    assert facts[DAY1].quality == QUALITY_RECORDED
    assert facts[DAY2].quality == QUALITY_RECORDED
    assert facts[DAY1].arr_delay_s == 600
    assert facts[DAY2].arr_delay_s == 600
    assert facts[DAY1].supposed_arrival_utc == _epoch(1, 12)
    assert facts[DAY2].supposed_arrival_utc == _epoch(2, 11)


def test_repeated_import_does_not_duplicate_dated_runs_or_plan_receipts(session):
    _import_dates(session)
    _import_dates(session)
    trips = session.execute(select(Trip)).scalars().all()
    assert len(trips) == 2


def test_finalizer_creates_unobserved_facts_with_each_trip_date_schedule(session):
    _import_dates(session)
    run_finalize_tt(session, service_date=DAY1, force=True, now=_epoch(3, 0))
    run_finalize_tt(session, service_date=DAY2, force=True, now=_epoch(3, 0))
    trips = session.execute(select(Trip).order_by(Trip.service_date)).scalars().all()
    facts = [
        session.execute(select(TripStop).where(TripStop.trip_id == trip.id)).scalars().one()
        for trip in trips
    ]
    assert [fact.supposed_arrival_utc for fact in facts] == [_epoch(1, 12), _epoch(2, 11)]
    assert all(fact.actual_arrival_utc is None and fact.arr_delay_s is None for fact in facts)
