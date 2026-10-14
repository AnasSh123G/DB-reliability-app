"""Public ingestion contracts for immutable, service-date-scoped TT plans."""
from __future__ import annotations

from datetime import datetime
from datetime import date as date_type
from pathlib import Path
import hashlib

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
from dbrel.ingest.plan_import import import_plan
from dbrel.models import (
    FeedVersion, Station, StationStop, Train, Trip, TripScheduleSnapshot, TripStop, TtPoll,
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


def test_archive_response_time_not_sweep_max_controls_forecast_classification(session):
    db, _feed, stations, _train, trip = session
    scheduled = _at(DAY1, 15)
    record_plans(db, trip.id, PATH_A, [(_stop(arr=scheduled, dep=scheduled), _receipt())],
                 {EVA_A: stations[0].id})
    response_time, sweep_time = _at(DAY1, 14), _at(DAY1, 16)
    result = run_tt_poll(db, _Changes(_changes_xml('pt="2609011500"'), response_time),
                         priority_max=99, now=sweep_time)
    poll = db.scalar(select(TtPoll))
    assert poll.sweep_id == sweep_time
    assert poll.fetched_at == response_time
    assert result.pending == 1
    assert result.stored == 0
    assert db.scalar(select(func.count()).select_from(TripStop)) == 0


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


def test_late_full_fchg_for_known_trip_adds_immutable_fchg_plan_receipt(session):
    db, _feed, stations, _train, trip = session
    trip.n_stops = 5
    trip.endpoints_tracked = 1
    old = _at(DAY1, 15)
    record_plans(db, trip.id, PATH_A, [(_stop(arr=old, dep=old), _receipt())],
                 {EVA_A: stations[0].id})
    identity_before = (trip.path_sha1, trip.endpoints_tracked, trip.n_stops)
    planned = _at(DAY1, 16)
    body = _changes_xml('pt="2609011600" ct="2609011610" l="RE42"', include_train=True)
    observed = _at(DAY1, 16, 20)
    result = run_tt_poll(db, _Changes(body, observed), priority_max=99, now=observed)
    fchg_rows = db.scalars(select(TripScheduleSnapshot).where(
        TripScheduleSnapshot.trip_id == trip.id,
        TripScheduleSnapshot.source_kind == "fchg",
    )).all()
    assert result.matched == 1
    assert len(fchg_rows) == 1
    assert fchg_rows[0].observed_at == observed
    assert fchg_rows[0].supposed_arrival_utc == planned
    assert fchg_rows[0].source_content_sha256 == hashlib.sha256(body).hexdigest()
    assert (trip.path_sha1, trip.endpoints_tracked, trip.n_stops) == identity_before, \
        "a one-stop full fchg must not rewrite established trip path or route endpoints"


def test_late_conflict_suppresses_recorded_arrival_delay_and_trip_rollup(session):
    db, _feed, stations, _train, trip = session
    original_arrival = _at(DAY1, 15)
    record_plans(db, trip.id, PATH_A,
                 [(_stop(arr=original_arrival), _receipt(observed=100, sha="a" * 64))],
                 {EVA_A: stations[0].id})
    actual_arrival = _at(DAY1, 15, 10)
    poll_time = actual_arrival + 180
    response = _Changes(_changes_xml('ct="2609011510" l="RE42"', include_departure=False), poll_time)
    run_tt_poll(db, response, priority_max=99, now=poll_time)
    fact = db.scalar(select(TripStop).where(TripStop.trip_id == trip.id, TripStop.stop_sequence == 1))
    assert fact.actual_arrival_utc == actual_arrival
    assert fact.arr_delay_s == 600

    conflict = _at(DAY1, 15, 5)
    record_plans(db, trip.id, PATH_A,
                 [(_stop(arr=conflict), _receipt(observed=poll_time + 1, sha="b" * 64))],
                 {EVA_A: stations[0].id})
    from dbrel.ingest.finalize_tt import run_finalize_tt

    run_finalize_tt(db, service_date=DAY1, force=True, now=poll_time + 1000)
    db.refresh(fact)
    db.refresh(trip)
    assert fact.quality == "RECORDED", "finalization must retain the known actual observation"
    assert fact.actual_arrival_utc == actual_arrival
    assert fact.arr_delay_s is None, "conflicted arrival plan cannot retain a stale +600 delay"
    assert trip.final_delay_s is None, "trip rollup must not publish delay from an ambiguous plan"


def test_replay_keeps_every_conflicting_plan_response_for_one_bucket(session, monkeypatch, tmp_path):
    from dbrel.ingest import archive
    from dbrel.ingest.archive import API_PLAN, ArchivedResponse
    from dbrel.ingest.replay import replay_day

    first_body = (f'<timetable station="Alpha" eva="{EVA_A}"><s id="123456789-2609011200-1">'
                  '<tl f="N" t="p" o="OP" c="RE" n="42"/>'
                  '<ar pt="2609011200" ppth="Beta"/><dp pt="2609011200" ppth="Beta"/>'
                  '</s></timetable>').encode()
    second_body = first_body.replace(b"2609011200", b"2609011205")
    responses = [
        ArchivedResponse(_at(DAY1, 11, 50), API_PLAN, EVA_A, first_body, "260901", 12),
        ArchivedResponse(_at(DAY1, 12, 20), API_PLAN, EVA_A, second_body, "260901", 12),
    ]
    path = tmp_path / "2026-09-01_12.parquet"
    monkeypatch.setattr(archive, "files_for_date", lambda _store, _day: [path])
    monkeypatch.setattr(archive, "missing_hours", lambda *_args: [])
    monkeypatch.setattr(archive, "covered_hours", lambda _paths: {(date_type(2026, 9, 1), 12)})
    monkeypatch.setattr(archive, "read_partition", lambda _path: iter(responses))

    replay_day(session[0], object(), date_type(2026, 9, 1), tracked_evas={EVA_A})
    rows = session[0].scalars(select(TripScheduleSnapshot).order_by(
        TripScheduleSnapshot.observed_at)).all()
    assert len(rows) == 2, "same-hour archive records are distinct immutable schedule evidence"
    assert [row.observed_at for row in rows] == [responses[0].recorded_at, responses[1].recorded_at]
    assert [row.source_content_sha256 for row in rows] == [
        hashlib.sha256(first_body).hexdigest(), hashlib.sha256(second_body).hexdigest()]


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
