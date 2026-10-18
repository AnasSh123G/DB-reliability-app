"""Focused regressions for sharing replay-day setup between ingest phases.

These tests use only a tiny in-memory database and the archive-shaped fixtures
from ``test_archive_replay``. They do not read the local archive or contact IRIS.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.ingest.changes_poller import run_tt_poll
from dbrel.ingest.plan_import import import_plan
from dbrel.ingest.runs import tt_feed_version
from dbrel.models import (
    Station,
    StationStop,
    Train,
    Trip,
    TripScheduleSnapshot,
    TripStop,
    TtPlanFetch,
    TtPoll,
)

from test_archive_replay import AFTER, PUNCTUAL_EVA, SILENT_EVA, _Source


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                Station(
                    natural_key="dormagen@51.099,6.837",
                    name="Dormagen",
                    lat=51.099,
                    lon=6.837,
                    eva=PUNCTUAL_EVA,
                    tracked=1,
                    poll_priority=1,
                ),
                Station(
                    natural_key="lennestadt-meggen@51.116,8.053",
                    name="Lennestadt-Meggen",
                    lat=51.116,
                    lon=8.053,
                    eva=SILENT_EVA,
                    tracked=1,
                    poll_priority=2,
                ),
            ]
        )
        db.commit()
        yield db


def _train_key_selects(engine, callback):
    statements = Counter()

    def count(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if "train.identity_key" in normalized:
            statements["train_by_key"] += 1
        if "feed_version.kind" in normalized:
            statements["feed_version"] += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        callback(statements)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return statements


def test_day_context_reuses_identity_maps_across_plan_and_poll(session, monkeypatch):
    """A plan-created identity must be visible to the following poll phase."""
    from dbrel.ingest import replay

    assert hasattr(replay, "ReplayDayContext"), "replay-day context type is missing"
    stations = session.execute(select(Station).order_by(Station.id)).scalars().all()
    context = replay.ReplayDayContext(
        fv_id=tt_feed_version(session),
        train_by_key={},
        line_cache={},
        tracked_stations=stations,
    )
    source = _Source()

    plan_module = importlib.import_module("dbrel.ingest.plan_import")
    poll_module = importlib.import_module("dbrel.ingest.changes_poller")
    run_contexts = []
    real_plan_context = plan_module.RunContext
    real_poll_context = poll_module.RunContext

    def capture_plan_context(*args, **kwargs):
        run_contexts.append(kwargs)
        return real_plan_context(*args, **kwargs)

    def capture_poll_context(*args, **kwargs):
        run_contexts.append(kwargs)
        return real_poll_context(*args, **kwargs)

    monkeypatch.setattr(plan_module, "RunContext", capture_plan_context)
    monkeypatch.setattr(poll_module, "RunContext", capture_poll_context)

    import_plan(
        session,
        source,
        date="2026-08-28",
        hours=[3, 4],
        force=True,
        now=AFTER,
        day_ctx=context,
    )
    session.flush()
    train_map = context.train_by_key
    line_map = context.line_cache
    assert train_map, "the plan phase should populate the shared train map"

    run_tt_poll(session, source, priority_max=99, now=AFTER, day_ctx=context)

    assert len(run_contexts) == 2
    assert all(call["train_by_key"] is train_map for call in run_contexts)
    assert all(call["line_cache"] is line_map for call in run_contexts)
    assert context.train_by_key is train_map
    assert context.line_cache is line_map
    assert context.tracked_stations is stations
    assert all(session.get(Train, train_id) is not None for train_id in train_map.values())


def test_replay_constructs_one_context_for_each_day(session, monkeypatch):
    from dbrel.ingest import archive, replay

    assert hasattr(replay, "ReplayDayContext"), "replay-day context type is missing"
    constructed = []
    real_context = replay.ReplayDayContext

    def make_context(*args, **kwargs):
        context = real_context(*args, **kwargs)
        constructed.append(context)
        return context

    monkeypatch.setattr(replay, "ReplayDayContext", make_context)
    path = SimpleNamespace(name="day-partition.sqlite")
    monkeypatch.setattr(archive, "files_for_date", lambda _store, _day: [path])
    monkeypatch.setattr(archive, "missing_hours", lambda *_args: [])
    monkeypatch.setattr(archive, "read_partition", lambda _path: [])
    monkeypatch.setattr(archive, "covered_hours", lambda _paths: {(date(2026, 8, 28), 0)})
    monkeypatch.setattr(archive, "group_sweeps", lambda _responses: [])

    replay.replay_day(session, object(), date(2026, 8, 28), tracked_evas=set(), dry_run=True)
    replay.replay_day(session, object(), date(2026, 8, 29), tracked_evas=set(), dry_run=True)

    assert len(constructed) == 2
    assert constructed[0] is not constructed[1]


def test_none_context_keeps_live_per_call_queries(session):
    """The opt-in replay cache must not change the live call path."""
    source = _Source()

    def run(_counts):
        import_plan(
            session, source, date="2026-08-28", hours=[3, 4], force=True,
            now=AFTER,
        )
        session.flush()
        run_tt_poll(session, source, priority_max=99, now=AFTER)

    counts = _train_key_selects(session.get_bind(), run)
    assert counts["train_by_key"] == 2
    assert counts["feed_version"] == 2


def _seeded_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all(
        [
            Station(
                natural_key="dormagen@51.099,6.837", name="Dormagen", lat=51.099,
                lon=6.837, eva=PUNCTUAL_EVA, tracked=1, poll_priority=1,
            ),
            Station(
                natural_key="lennestadt-meggen@51.116,8.053", name="Lennestadt-Meggen",
                lat=51.116, lon=8.053, eva=SILENT_EVA, tracked=1, poll_priority=2,
            ),
        ]
    )
    db.commit()
    return db


def _stable_ingest_rows(session):
    """Natural and grading fields, excluding wall-clock profiling columns."""
    models_and_fields = (
        (Train, ("id", "identity_key", "source", "n_runs_seen", "n_stops_tracked")),
        (Trip, ("id", "train_id", "service_date", "path_sha1", "finalized_at")),
        (StationStop, ("id", "train_id", "stop_sequence", "station_id")),
        (TripScheduleSnapshot, ("id", "trip_id", "stop_sequence", "source_kind", "observed_at", "source_content_sha256")),
        (TripStop, ("id", "trip_id", "stop_sequence", "quality", "actual_arrival_utc", "actual_departure_utc", "arr_delay_s", "dep_delay_s")),
        (TtPlanFetch, ("id", "eva", "plan_date", "plan_hour", "http_status", "n_stops")),
        (TtPoll, ("id", "sweep_id", "station_id", "eva", "http_status", "n_stops", "content_sha256")),
    )
    snapshots = {}
    for model, fields in models_and_fields:
        rows = session.execute(select(model).order_by(model.id)).scalars().all()
        snapshots[model.__tablename__] = [tuple(getattr(row, field) for field in fields) for row in rows]
    return snapshots


def test_day_context_and_live_path_produce_same_ingest_rows(session):
    """The day cache changes setup cost, not the rows produced by real ingest."""
    context_session = _seeded_session()
    live_session = _seeded_session()
    try:
        stations = context_session.execute(select(Station).order_by(Station.id)).scalars().all()
        context = importlib.import_module("dbrel.ingest.replay").ReplayDayContext(
            fv_id=tt_feed_version(context_session),
            train_by_key={},
            line_cache={},
            tracked_stations=stations,
        )
        source = _Source()
        for db, day_ctx in ((context_session, context), (live_session, None)):
            kwargs = {} if day_ctx is None else {"day_ctx": day_ctx}
            import_plan(
                db, source, date="2026-08-28", hours=[3, 4], force=True,
                now=AFTER, **kwargs,
            )
            db.flush()
            run_tt_poll(db, source, priority_max=99, now=AFTER, **kwargs)
            db.flush()

        assert _stable_ingest_rows(context_session) == _stable_ingest_rows(live_session)
    finally:
        context_session.close()
        live_session.close()


