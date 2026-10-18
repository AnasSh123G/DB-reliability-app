"""Small database regressions for deferred and batched plan writes (D6.3–D6.5)."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
import pytest

from dbrel.db import Base
from dbrel.ingest.changes_poller import run_tt_poll
from dbrel.ingest.dated_schedule import PlanReceipt, record_plans
from dbrel.ingest.plan_import import import_plan
from dbrel.ingest.runs import RunContext, RunCounters, tt_feed_version, upsert_run
from dbrel.models import Station, Trip, TripScheduleSnapshot, TripStop
from test_archive_replay import (
    AFTER,
    PUNCTUAL_EVA,
    PUNCTUAL_PLAN_STOP,
    SILENT_EVA,
    _Source,
    _plan_xml,
)
from test_upsert_run_collision import _stop


def _new_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(
        Station(
            natural_key="bochum@51.479,7.223",
            name="Bochum Hbf",
            lat=51.479,
            lon=7.223,
            eva=8000041,
            tracked=1,
        )
    )
    db.flush()
    return db


def _run_context(session):
    station = session.scalar(select(Station).where(Station.eva == 8000041))
    return RunContext(
        fv_id=tt_feed_version(session),
        station_id_by_eva={8000041: station.id},
        station_name_by_eva={8000041: station.name},
        train_by_key={},
    )


def test_record_plans_resolves_all_station_stop_ids_in_one_query():
    db = _new_session()
    try:
        station = db.scalar(select(Station).where(Station.eva == 8000041))
        ctx = _run_context(db)
        stops = [_stop(daily_trip_id="multi", start_stamp="2609160800", stop_index=i) for i in (1, 2, 3)]
        trip_id = upsert_run(db, ctx, RunCounters(), stops[0].run_key, stops)
        trip = db.get(Trip, trip_id)
        receipt = PlanReceipt(observed_at=1_800_000_000, content_sha256="a" * 64)
        queries = Counter()

        def count(_conn, _cursor, statement, _parameters, _context, _many):
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select station_stop.id"):
                queries["station_stop_ids"] += 1

        event.listen(db.get_bind(), "before_cursor_execute", count)
        try:
            record_plans(
                db, trip.id, trip.path_sha1,
                [(stop, receipt) for stop in stops],
                {8000041: station.id},
            )
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", count)

        assert queries["station_stop_ids"] <= 1
    finally:
        db.close()


def test_record_plans_keeps_receipt_validation_order_and_message():
    db = _new_session()
    try:
        ctx = _run_context(db)
        stop = _stop(daily_trip_id="validation", start_stamp="2609160800")
        trip_id = upsert_run(db, ctx, RunCounters(), stop.run_key, [stop])
        trip = db.get(Trip, trip_id)
        wrong_day = replace(stop, start_stamp="2609170800")
        invalid_receipt = PlanReceipt(observed_at=-1, content_sha256="not-a-digest")

        with pytest.raises(ValueError, match="^receipt observed_at must be a nonnegative integer$"):
            record_plans(db, trip.id, trip.path_sha1, [(wrong_day, invalid_receipt)], {8000041: 1})
    finally:
        db.close()


def test_pending_settle_matches_immediate_writes_across_path_repoint():
    """Both trip lookup branches settle old path facts before repointing."""
    from dbrel.ingest.schedule_reconcile import PendingSettle

    def ingest(deferred, *, source_key_conflict):
        db = _new_session()
        try:
            station = db.scalar(select(Station).where(Station.eva == 8000041))
            ctx = _run_context(db)
            pending = PendingSettle() if deferred else None
            counters = RunCounters()
            first = _stop(daily_trip_id="old-path", start_stamp="2609160800")
            first = replace(first, departure=replace(first.departure, planned_ts=1_800_000_000))
            trip_id = upsert_run(db, ctx, counters, first.run_key, [first])
            # The fact predates the first scheduled write in this deferred epoch.
            db.add(
                TripStop(
                    trip_id=trip_id,
                    stop_sequence=1,
                    station_id=station.id,
                    actual_departure_utc=1_800_000_120,
                    quality="RECORDED",
                    rule_version=3,
                    cancelled=0,
                )
            )
            db.flush()
            receipt_a = PlanReceipt(observed_at=1_800_000_001, content_sha256="a" * 64)
            upsert_run(
                db, ctx, counters, first.run_key, [first],
                schedule_observations=[(first, receipt_a)], pending=pending,
            )

            # The second path adds a different stop sequence. For the source-key
            # branch, the changed operator also mints a different train identity,
            # so the existing (source, run_key, service_date) row is the conflict.
            ctx.station_name_by_eva[8000041] = "Essen Hbf"
            second = _stop(
                daily_trip_id="old-path" if source_key_conflict else "new-path",
                start_stamp="2609160800",
                stop_index=2,
            )
            if source_key_conflict:
                second = replace(second, owner="OTHER")
            second = replace(second, departure=replace(second.departure, planned_ts=1_800_000_060))
            receipt_b = PlanReceipt(observed_at=1_800_000_061, content_sha256="b" * 64)
            repointed_trip_id = upsert_run(
                db, ctx, counters, second.run_key, [second],
                schedule_observations=[(second, receipt_b)], pending=pending,
            )
            assert repointed_trip_id == trip_id
            if source_key_conflict:
                assert len(set(ctx.train_by_key.values())) == 2, "conflict route uses a distinct train identity"
            if pending is not None:
                pending.settle(db)
            db.flush()
            trip = db.scalar(select(Trip))
            snapshots = db.execute(select(TripScheduleSnapshot).order_by(TripScheduleSnapshot.id)).scalars().all()
            trip_row = tuple(getattr(trip, column.name) for column in Trip.__table__.columns)
            fact_rows = [
                tuple(getattr(row, column.name) for column in TripStop.__table__.columns)
                for row in db.execute(select(TripStop).order_by(TripStop.id)).scalars()
            ]
            snapshot_rows = [
                tuple(getattr(row, column.name) for column in TripScheduleSnapshot.__table__.columns)
                for row in snapshots
            ]
            return (
                trip_row,
                fact_rows,
                snapshot_rows,
            )
        finally:
            db.close()

    for source_key_conflict in (False, True):
        immediate = ingest(False, source_key_conflict=source_key_conflict)
        deferred = ingest(True, source_key_conflict=source_key_conflict)
        assert immediate == deferred, f"source_key_conflict={source_key_conflict}"


def test_pending_settle_coalesces_sequences_and_selectively_settles(monkeypatch):
    from dbrel.ingest import schedule_reconcile
    from dbrel.ingest.schedule_reconcile import PendingSettle

    db = _new_session()
    try:
        ctx = _run_context(db)
        first = _stop(daily_trip_id="first", start_stamp="2609160800")
        second = _stop(daily_trip_id="second", start_stamp="2609170800")
        first_id = upsert_run(db, ctx, RunCounters(), first.run_key, [first])
        second_id = upsert_run(db, ctx, RunCounters(), second.run_key, [second])
        bounds_calls = Counter()
        reconcile_calls = Counter()

        def refresh(_session, trip):
            bounds_calls[trip.id] += 1

        def reconcile(_session, trip, sequences, **_kwargs):
            reconcile_calls[trip.id] += 1
            assert sequences == {1, 2, 3}

        monkeypatch.setattr(schedule_reconcile, "refresh_run_bounds", refresh)
        monkeypatch.setattr(schedule_reconcile, "reconcile_facts", reconcile)
        pending = PendingSettle()
        pending.add(first_id, {1})
        pending.add(first_id, {2, 3})
        pending.add(first_id, set())
        pending.add(second_id, set())

        pending.settle(db, [first_id])
        assert pending.dirty == {second_id: set()}
        assert bounds_calls == {first_id: 1}
        assert reconcile_calls == {first_id: 1}
        pending.settle(db)
        assert pending.dirty == {}
        assert bounds_calls == {first_id: 1, second_id: 1}
        assert reconcile_calls == {first_id: 1}
    finally:
        db.close()


def test_poll_hashes_one_response_body_once_even_for_full_record(monkeypatch):
    """The poll checksum is reused for each receipt from that response."""
    from dbrel.ingest import changes_poller

    db = _new_session()
    try:
        # Add the second tracked station expected by the local archive-shaped source.
        db.add(
            Station(
                natural_key="dormagen@51.099,6.837", name="Dormagen", lat=51.099,
                lon=6.837, eva=PUNCTUAL_EVA, tracked=1,
            )
        )
        db.add(
            Station(
                natural_key="lennestadt-meggen@51.116,8.053", name="Lennestadt-Meggen",
                lat=51.116, lon=8.053, eva=SILENT_EVA, tracked=1,
            )
        )
        db.flush()
        source = _Source()
        response_body = _plan_xml(
            PUNCTUAL_EVA,
            "Dormagen",
            PUNCTUAL_PLAN_STOP,
        )
        source._fchg[PUNCTUAL_EVA] = response_body
        original_sha256 = changes_poller.hashlib.sha256
        hashes = Counter()

        def count_hash(data=b"", *args, **kwargs):
            if data == response_body:
                hashes["response"] += 1
            return original_sha256(data, *args, **kwargs)

        import_plan(db, source, date="2026-08-28", hours=[3, 4], force=True, now=AFTER)
        db.flush()
        monkeypatch.setattr(changes_poller.hashlib, "sha256", count_hash)
        run_tt_poll(db, source, priority_max=99, now=AFTER)

        assert hashes["response"] == 1
    finally:
        db.close()
