"""Public ingestion contracts for immutable, service-date-scoped TT plans."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dbrel.config import TZ
from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.ingest.changes_poller import run_tt_poll
from dbrel.ingest.client import Response
from dbrel.ingest.dated_schedule import PlanReceipt, load_patterns, record_plans
from dbrel.ingest.parser import TtEvent, TtStop
from dbrel.models import (
    FeedVersion, Station, StationStop, Train, Trip, TripScheduleSnapshot, TripStop,
)

EVA_A, EVA_B = 8_000_101, 8_000_102
DAY1, DAY2 = "2026-09-01", "2026-09-02"
PATH_A, PATH_B = "a" * 40, "b" * 40


@pytest.fixture
def session():
    engine = __import__("sqlalchemy").create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        feed = FeedVersion(kind="rv", source_url="fixture", content_sha256="f" * 64, downloaded_at=1)
        stations = [
            Station(natural_key="a@1,1", name="Alpha", lat=1, lon=1, eva=EVA_A, tracked=1),
            Station(natural_key="b@2,2", name="Beta", lat=2, lon=2, eva=EVA_B, tracked=0),
        ]
        train = Train(source=SOURCE_TT, identity_kind="NUMBERED", identity_key="tts:RE/42",
                      category="RE", train_number=42, path_sha1=PATH_A, route_path="Alpha|Beta")
        db.add_all([feed, *stations, train]); db.flush()
        trip = Trip(train_id=train.id, service_date=DAY1, source=SOURCE_TT,
                    source_run_key="123456789-2609011200", path_sha1=PATH_A,
                    feed_version_id=feed.id, supposed_arrival_utc=_at(DAY1, 15),
                    supposed_departure_utc=_at(DAY1, 15))
        db.add(trip)
        db.add(StationStop(feed_version_id=feed.id, train_id=train.id, path_sha1=PATH_A,
                           stop_sequence=1, station_id=stations[0].id,
                           planned_arrival_sec=15 * 3600, planned_departure_sec=15 * 3600))
        db.flush()
        yield db, feed, stations, train, trip
    engine.dispose()


def _at(day: str, hour: int, minute: int = 0) -> int:
    y, m, d = map(int, day.split("-"))
    return int(datetime(y, m, d, hour, minute, tzinfo=TZ).timestamp())


def _event(arr: int | None, dep: int | None, *, changed_arr=None, changed_dep=None) -> tuple:
    def side(planned, changed):
        return TtEvent(planned, changed, "2", None, "p", None, None, None,
                       "RE42", None, False, None, None)
    return side(arr, changed_arr), side(dep, changed_dep)


def _stop(*, day=DAY1, seq=1, eva=EVA_A, arr=None, dep=None, changed_arr=None,
          changed_dep=None, sid=None):
    stamp = "2609011200" if day == DAY1 else "2609022350"
    arrival, departure = _event(arr, dep, changed_arr=changed_arr, changed_dep=changed_dep)
    return TtStop(sid or f"123456789-{stamp}-{seq}", eva, "123456789", stamp, seq,
                  "RE", "42", "OP", "p", None, "RE 42", arrival, departure)


def _receipt(*, observed=100, observed_at=None, sha="a" * 64, kind="plan", eva=EVA_A,
             day=DAY1, hour=12):
    if observed_at is not None:
        observed = observed_at
    return PlanReceipt(observed, sha, kind, eva, day, hour)


@pytest.mark.parametrize(
    "plan,stop_kwargs,mapping",
    [
        (dict(observed_at=-1), {}, {EVA_A: 1}),
        (dict(observed_at=True), {}, {EVA_A: 1}),
        (dict(sha="bad"), {}, {EVA_A: 1}),
        (dict(kind="other"), {}, {EVA_A: 1}),
        (dict(day="not-a-date"), {}, {EVA_A: 1}),
        (dict(day="2026-02-30"), {}, {EVA_A: 1}),
        (dict(hour=24), {}, {EVA_A: 1}),
        (dict(hour=True), {}, {EVA_A: 1}),
        ({}, {"seq": 0}, {EVA_A: 1}),
        ({}, {"arr": -1}, {EVA_A: 1}),
        ({}, {"arr": 1.5}, {EVA_A: 1}),
        ({"eva": EVA_B}, {}, {EVA_A: 1}),
        ({}, {"eva": EVA_B}, {EVA_A: 1}),
        ({}, {"eva": None}, {EVA_A: 1}),
    ],
)
def test_invalid_receipt_schedule_and_station_inputs_are_rejected(session, plan, stop_kwargs, mapping):
    db, _feed, stations, _train, trip = session
    station_map = {EVA_A: stations[0].id, EVA_B: stations[1].id} if mapping else {}
    # Fixture IDs in mapping cases are resolved from the actual synthetic station rows.
    if EVA_A in mapping:
        station_map[EVA_A] = stations[0].id
    if EVA_B in mapping:
        station_map[EVA_B] = stations[1].id
    observation = _stop(**stop_kwargs)
    receipt = _receipt(**plan)
    with pytest.raises(ValueError):
        record_plans(db, trip.id, PATH_A, [(observation, receipt)], station_map)
    assert db.scalar(select(func.count()).select_from(TripScheduleSnapshot)) == 0


class _Changes:
    def __init__(self, body: bytes, recorded_at: int):
        self.body, self.recorded_at = body, recorded_at

    def changes(self, eva):
        return Response(status=200, body=self.body, elapsed_ms=0, recorded_at=self.recorded_at)

    def plan(self, eva, date, hour):
        return Response(status=404, body=b"", elapsed_ms=0)


def _changes_xml(arr_attributes: str, dep_attributes: str | None = None, *,
                 include_train=False, include_departure=True) -> bytes:
    train = '<tl f="N" t="p" o="OP" c="RE" n="42"/>' if include_train else ""
    dep_attributes = arr_attributes if dep_attributes is None else dep_attributes
    return (f'<timetable eva="{EVA_A}"><s id="123456789-2609011200-1" eva="{EVA_A}">'
            f'{train}<ar {arr_attributes}/>'
            f'{f"<dp {dep_attributes}/>" if include_departure else ""}'
            f'</s></timetable>').encode()


def test_conflicting_arrival_schedule_keeps_actual_and_known_departure(session):
    db, _feed, stations, _train, trip = session
    arr_a, arr_b, dep = _at(DAY1, 15), _at(DAY1, 15, 5), _at(DAY1, 15, 10)
    record_plans(db, trip.id, PATH_A,
                 [(_stop(arr=arr_a, dep=dep), _receipt(observed=100, sha="a" * 64)),
                  (_stop(arr=arr_b, dep=dep), _receipt(observed=101, sha="b" * 64))],
                 {EVA_A: stations[0].id})
    actual_arr, actual_dep = _at(DAY1, 15, 2), _at(DAY1, 15, 12)
    now = actual_dep + 180
    arr_attrs, dep_attrs = 'ct="2609011502" l="RE42"', 'ct="2609011512" l="RE42"'
    run_tt_poll(db, _Changes(_changes_xml(arr_attrs, dep_attrs), now), priority_max=99, now=now)
    fact = db.scalar(select(TripStop).where(TripStop.trip_id == trip.id, TripStop.stop_sequence == 1))
    assert fact.actual_arrival_utc == actual_arr
    assert fact.arr_delay_s is None, "ambiguous planned arrival must suppress only its delay"
    assert fact.actual_departure_utc == actual_dep
    assert fact.dep_delay_s == 120


def test_snapshots_are_append_only_and_exact_replays_are_idempotent(session):
    db, _feed, stations, _train, trip = session
    first = (_stop(arr=_at(DAY1, 15), dep=_at(DAY1, 15)), _receipt())
    assert record_plans(db, trip.id, PATH_A, [first], {EVA_A: stations[0].id}) == 1
    assert record_plans(db, trip.id, PATH_A, [first], {EVA_A: stations[0].id}) == 0
    revised = (_stop(arr=_at(DAY1, 16), dep=_at(DAY1, 16)),
               _receipt(observed=200, sha="b" * 64))
    assert record_plans(db, trip.id, PATH_A, [revised], {EVA_A: stations[0].id}) == 1
    rows = db.scalars(select(TripScheduleSnapshot).where(
        TripScheduleSnapshot.trip_id == trip.id).order_by(TripScheduleSnapshot.observed_at)).all()
    assert [(row.observed_at, row.supposed_arrival_utc) for row in rows] == [
        (100, _at(DAY1, 15)), (200, _at(DAY1, 16))]


def test_repeated_station_and_midnight_path_variants_remain_date_scoped(session):
    db, feed, stations, train, first_trip = session
    second_trip = Trip(train_id=train.id, service_date=DAY2, source=SOURCE_TT,
                       source_run_key="123456789-2609022350", path_sha1=PATH_B,
                       feed_version_id=feed.id, supposed_arrival_utc=_at(DAY2, 23, 50) + 900)
    db.add(second_trip); db.flush()
    midnight = _at(DAY2, 23, 50) + 900
    observations = [
        (_stop(day=DAY1, seq=1, eva=EVA_A, arr=_at(DAY1, 23, 50)),
         _receipt(observed=100, sha="a" * 64, day=DAY1)),
        (_stop(day=DAY2, seq=1, eva=EVA_B, arr=_at(DAY2, 23, 50)),
         _receipt(observed=200, sha="b" * 64, eva=EVA_B, day=DAY2, hour=23)),
        (_stop(day=DAY2, seq=3, eva=EVA_A, arr=midnight),
         _receipt(observed=201, sha="c" * 64, day=DAY2, hour=23)),
    ]
    assert record_plans(db, first_trip.id, PATH_A, [observations[0]],
                        {EVA_A: stations[0].id, EVA_B: stations[1].id}) == 1
    assert record_plans(db, second_trip.id, PATH_B, observations[1:],
                        {EVA_A: stations[0].id, EVA_B: stations[1].id}) == 2
    plans = load_patterns(db, [train.id], {train.id: {DAY1: PATH_A, DAY2: PATH_B}})
    assert plans[(train.id, DAY1)][1].station_id == stations[0].id
    assert set(plans[(train.id, DAY2)]) == {1, 3}
    assert plans[(train.id, DAY2)][1].station_id == stations[1].id
    assert plans[(train.id, DAY2)][3].station_id == stations[0].id
    assert plans[(train.id, DAY2)][3].supposed_arr == midnight
    assert midnight > _at(DAY2, 23, 59)
