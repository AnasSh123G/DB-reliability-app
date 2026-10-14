"""Regression tests for dated schedule receipt, DST and route refresh edges."""
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
from dbrel.ingest.dated_schedule import DatedPlanned, PlanReceipt, load_patterns, record_plans
from dbrel.ingest.parser import parse_timetable, parse_ts
from dbrel.ingest.plan_import import import_plan
from dbrel.models import FeedVersion, Station, Train, Trip, TripScheduleSnapshot, TripStop

EVA_A, EVA_B = 8_010_101, 8_010_102
DAY = "2026-09-01"
PATH = "c" * 40


@pytest.fixture
def db_and_trip():
    engine = __import__("sqlalchemy").create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        feed = FeedVersion(kind="rv", source_url="fixture", content_sha256="e" * 64,
                           downloaded_at=1)
        stations = [
            Station(natural_key="a@1,1", name="Alpha", lat=1, lon=1, eva=EVA_A, tracked=1),
            Station(natural_key="b@2,2", name="Beta", lat=2, lon=2, eva=EVA_B, tracked=0),
        ]
        train = Train(source=SOURCE_TT, identity_kind="NUMBERED", identity_key="tts:RE/42",
                      category="RE", train_number=42, path_sha1=PATH, route_path="Alpha|Beta")
        db.add_all([feed, *stations, train]); db.flush()
        trip = Trip(train_id=train.id, service_date=DAY, source=SOURCE_TT,
                    source_run_key="123456789-2609011200", path_sha1=PATH,
                    feed_version_id=feed.id)
        db.add(trip); db.flush()
        yield db, feed, stations, train, trip
    engine.dispose()


def _epoch(day: str, hour: int, minute: int = 0) -> int:
    year, month, day_num = map(int, day.split("-"))
    return int(datetime(year, month, day_num, hour, minute, tzinfo=TZ).timestamp())


def _stop_xml(*, eva=EVA_A, seq=1, day=DAY, stamp="2609011200", arr="2609011000",
              dep=None, arr_pp="1", dep_pp=None, before="", after="") -> bytes:
    dep = arr if dep is None else dep
    tl = '<tl f="N" t="p" o="OP" c="RE" n="42"/>'
    arrival = f'<ar pt="{arr}" pp="{arr_pp}" ppth="{before}"/>'
    departure = f'<dp pt="{dep}" pp="{dep_pp or arr_pp}" ppth="{after}"/>' if dep else ""
    return (f'<timetable eva="{eva}"><s id="123456789-{stamp}-{seq}" eva="{eva}">'
            f'{tl}{arrival}{departure}</s></timetable>').encode()


class _ChangeResponse:
    def __init__(self, body: bytes, recorded_at: int):
        self.body, self.recorded_at = body, recorded_at

    def changes(self, _eva):
        return Response(status=200, body=self.body, elapsed_ms=0, recorded_at=self.recorded_at)


def _receipt(at: int, digest: str, hour=9) -> PlanReceipt:
    return PlanReceipt(at, digest * 64, "plan", EVA_A, DAY, hour)


def test_poll_uses_only_plan_receipts_observed_by_fchg_time(db_and_trip):
    db, _feed, stations, _train, trip = db_and_trip
    earlier = _epoch(DAY, 10, 0)
    later = _epoch(DAY, 10, 10)
    record_plans(db, trip.id, PATH,
                 [(parse_timetable(_stop_xml()).stops[0], _receipt(_epoch(DAY, 9), "a"))],
                 {EVA_A: stations[0].id})
    # The later plan was genuinely received after this fchg payload was captured.
    record_plans(db, trip.id, PATH,
                 [(parse_timetable(_stop_xml(arr="2609011010")).stops[0],
                   _receipt(_epoch(DAY, 12, 30), "b", 12))],
                 {EVA_A: stations[0].id})
    observed = _epoch(DAY, 12, 20)
    body = (f'<timetable eva="{EVA_A}"><s id="123456789-2609011200-1" eva="{EVA_A}">'
            '<ar ct="2609011005" l="RE42"/></s></timetable>').encode()

    run_tt_poll(db, _ChangeResponse(body, observed), priority_max=99,
                now=_epoch(DAY, 12, 25))
    fact = db.scalar(select(TripStop).where(TripStop.trip_id == trip.id))
    assert fact is not None
    assert fact.supposed_arrival_utc == earlier
    assert fact.supposed_arrival_utc != later
    assert fact.arr_delay_s == 300


def test_overlapping_dst_plan_receipts_share_one_corrected_schedule(db_and_trip):
    db, _feed, stations, _train, _trip = db_and_trip
    stations[1].tracked = 0
    autumn = "2026-10-25"
    stamp = "2610250230"
    first = (
        f'<timetable eva="{EVA_A}">'
        f'<s id="123456789-{stamp}-1" eva="{EVA_A}"><tl f="N" t="p" o="OP" c="RE" n="42"/>'
        '<ar pt="2610250250" pp="1"/><dp pt="2610250250" pp="1"/></s>'
        f'<s id="123456789-{stamp}-2" eva="{EVA_A}"><tl f="N" t="p" o="OP" c="RE" n="42"/>'
        '<ar pt="2610250210" pp="1"/><dp pt="2610250210" pp="1"/></s></timetable>'
    ).encode()

    class TwoOverlappingPlans:
        def __init__(self):
            self.calls = 0

        def plan(self, _eva, _date, _hour):
            self.calls += 1
            return Response(status=200, body=first, elapsed_ms=0, recorded_at=100 + self.calls)

    import_plan(db, TwoOverlappingPlans(), date=autumn, hours=[1, 2], force=True,
                workers=1, now=_epoch(autumn, 1))
    trip = db.scalar(select(Trip).where(Trip.source_run_key == f"123456789-{stamp}"))
    assert trip is not None
    rows = db.scalars(select(TripScheduleSnapshot).where(
        TripScheduleSnapshot.trip_id == trip.id,
        TripScheduleSnapshot.stop_sequence == 2,
    ).order_by(TripScheduleSnapshot.observed_at)).all()
    corrected = parse_ts("2610250210") + 3600
    assert len(rows) == 2
    assert [row.supposed_arrival_utc for row in rows] == [corrected, corrected]
    pattern = load_patterns(db, [trip.train_id], {trip.train_id: {autumn: trip.path_sha1}})
    assert pattern[(trip.train_id, autumn)][2].supposed_arr == corrected


def test_planned_platform_only_poll_change_is_persisted(db_and_trip, monkeypatch):
    import dbrel.ingest.changes_poller as poller

    db, _feed, stations, _train, trip = db_and_trip
    scheduled = _epoch(DAY, 10)
    observed = _epoch(DAY, 12)
    actual_body = (f'<timetable eva="{EVA_A}"><s id="123456789-2609011200-1" eva="{EVA_A}">'
                   '<ar ct="2609011001" l="RE42"/></s></timetable>').encode()
    platform = "1"

    def fake_patterns(_session, train_ids, dates_by_train, *, as_of=None):
        return {(trip.train_id, DAY): {1: DatedPlanned(
            None, stations[0].id, platform, scheduled, None,
        )}}

    monkeypatch.setattr(poller, "_load_patterns", fake_patterns)
    run_tt_poll(db, _ChangeResponse(actual_body, observed), priority_max=99, now=observed)
    fact = db.scalar(select(TripStop).where(TripStop.trip_id == trip.id))
    assert fact is not None and fact.planned_platform == "1"

    platform = "2"
    result = run_tt_poll(db, _ChangeResponse(actual_body, observed + 1), priority_max=99,
                         now=observed + 1)
    db.refresh(fact)
    assert result.stored == 1
    assert fact.planned_platform == "2"


def test_cancellation_without_observed_time_keeps_actual_sides_null(db_and_trip, monkeypatch):
    import dbrel.ingest.changes_poller as poller

    db, _feed, stations, _train, trip = db_and_trip
    scheduled = _epoch(DAY, 10)
    observed = _epoch(DAY, 12)
    body = (f'<timetable eva="{EVA_A}"><s id="123456789-2609011200-1" eva="{EVA_A}">'
            '<ar pt="2609011000" cs="c" clt="2609010955"/></s></timetable>').encode()

    def patterns(_session, _train_ids, _dates_by_train, *, as_of=None):
        return {(trip.train_id, DAY): {1: DatedPlanned(
            None, stations[0].id, None, scheduled, None,
        )}}

    monkeypatch.setattr(poller, "_load_patterns", patterns)
    run_tt_poll(db, _ChangeResponse(body, observed), priority_max=99, now=observed)
    fact = db.scalar(select(TripStop).where(TripStop.trip_id == trip.id))
    assert fact is not None
    assert fact.quality == "CANCELLED"
    assert fact.arr_cancelled == 1
    assert fact.actual_arrival_utc is None
    assert fact.actual_departure_utc is None
    assert fact.arr_delay_s is None
    assert fact.dep_delay_s is None


class _PlansByEva:
    def __init__(self, answers):
        self.answers = answers

    def plan(self, eva, _date, _hour):
        return self.answers[eva]


def test_partial_plan_refresh_preserves_known_route_bounds(db_and_trip):
    db, _feed, stations, _train, _trip = db_and_trip
    stations[1].tracked = 1
    origin = _stop_xml(eva=EVA_A, seq=1, arr="2609011500", dep="2609011500",
                       after="Beta")
    destination = _stop_xml(eva=EVA_B, seq=2, arr="2609011600", dep="2609011600",
                            before="Alpha")
    ok_origin = Response(status=200, body=origin, elapsed_ms=0, recorded_at=100)
    ok_destination = Response(status=200, body=destination, elapsed_ms=0, recorded_at=100)
    import_plan(db, _PlansByEva({EVA_A: ok_origin, EVA_B: ok_destination}),
                date=DAY, hours=[15], force=True, workers=1, now=_epoch(DAY, 15))
    trip = db.scalar(select(Trip).where(Trip.source == SOURCE_TT, Trip.service_date == DAY))
    assert trip is not None
    assert trip.n_stops_tracked == 2
    end_before = trip.supposed_arrival_utc
    assert end_before == _epoch(DAY, 16)

    partial = Response(status=200, body=origin, elapsed_ms=0, recorded_at=200)
    missing = Response(status=404, body=b"", elapsed_ms=0, recorded_at=200)
    import_plan(db, _PlansByEva({EVA_A: partial, EVA_B: missing}),
                date=DAY, hours=[15], force=True, workers=1, now=_epoch(DAY, 15, 5))
    db.refresh(trip)
    assert trip.n_stops_tracked == 2
    assert trip.supposed_arrival_utc == end_before
