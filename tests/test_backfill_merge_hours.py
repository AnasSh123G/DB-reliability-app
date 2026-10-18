"""Opt-in grouping of archived plan hours during backfill."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from dbrel import cli
from dbrel.db import Base
from dbrel.ingest.archive import API_PLAN, ArchivedResponse
from dbrel.ingest.replay import ReplayResult
from dbrel.models import Station


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Station(
        natural_key="merge-test", name="Merge test", lat=0, lon=0,
        eva=8000001, tracked=1,
    ))
    session.flush()
    return engine, session


def _plan(hour: int, recorded_at: int, body: bytes = b"plan"):
    return ArchivedResponse(
        recorded_at=recorded_at, api_name=API_PLAN, eva=8000001,
        body=body, plan_date="260510", plan_hour=hour,
    )


def test_merge_flag_imports_all_partition_hours_once_and_uses_latest_recording(monkeypatch):
    from dbrel.ingest import archive, plan_import, replay

    engine, session = _session()
    try:
        path = SimpleNamespace(name="partition.parquet")
        monkeypatch.setattr(archive, "files_for_date", lambda *_: [path])
        monkeypatch.setattr(archive, "missing_hours", lambda *_: [])
        monkeypatch.setattr(archive, "covered_hours", lambda *_: {(date(2026, 5, 10), 3)})
        monkeypatch.setattr(archive, "group_sweeps", lambda _: [])
        responses = [_plan(7, 100), _plan(3, 300), _plan(7, 200)]
        monkeypatch.setattr(archive, "read_partition", lambda _: responses)
        calls = []

        def fake_import(_session, source, **kwargs):
            calls.append((source, kwargs))
            return SimpleNamespace(
                upsert_calls=2, buckets_fetched=len(kwargs["hours"]),
                stops_seen=4, trips_created=1, errors=[],
            )

        monkeypatch.setattr(plan_import, "import_plan", fake_import)
        result = replay.replay_day(
            session, object(), date(2026, 5, 10), tracked_evas={8000001},
            dry_run=True, merge_plan_hours=True,
        )

        assert len(calls) == 1
        source, kwargs = calls[0]
        assert kwargs["hours"] == [3, 7]
        assert kwargs["now"] == 300
        assert source.plan(8000001, "260510", 7).body == b"plan"
        assert source.plan(8000001, "260510", 7).recorded_at == 200
        assert result.plan_buckets == 2
        assert result.plan_stops == 4
        assert result.upsert_calls == 2
    finally:
        session.close()
        engine.dispose()


def test_default_replay_keeps_one_import_per_plan_hour(monkeypatch):
    from dbrel.ingest import archive, plan_import, replay

    engine, session = _session()
    try:
        path = SimpleNamespace(name="partition.parquet")
        monkeypatch.setattr(archive, "files_for_date", lambda *_: [path])
        monkeypatch.setattr(archive, "missing_hours", lambda *_: [])
        monkeypatch.setattr(archive, "covered_hours", lambda *_: {(date(2026, 5, 10), 3)})
        monkeypatch.setattr(archive, "group_sweeps", lambda _: [])
        monkeypatch.setattr(archive, "read_partition", lambda _: [_plan(7, 100), _plan(3, 300)])
        calls = []

        def fake_import(_session, _source, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                upsert_calls=0, buckets_fetched=1, stops_seen=0,
                trips_created=0, errors=[],
            )

        monkeypatch.setattr(plan_import, "import_plan", fake_import)
        replay.replay_day(session, object(), date(2026, 5, 10), tracked_evas={8000001}, dry_run=True)
        assert [call["hours"] for call in calls] == [[3], [7]]
        assert [call["now"] for call in calls] == [300, 100]
    finally:
        session.close()
        engine.dispose()


def test_backfill_cli_forwards_merge_plan_hours(tmp_path, monkeypatch):
    from dbrel import db

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    db.dispose_engine()
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Station(
            natural_key="cli-merge", name="CLI merge", lat=0, lon=0,
            eva=8000001, tracked=1,
        ))
        session.commit()

    from contextlib import contextmanager

    @contextmanager
    def isolated_scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(cli, "session_scope", isolated_scope)
    received = []

    def fake_replay(*_args, **kwargs):
        received.append(kwargs["merge_plan_hours"])
        return ReplayResult(sweeps=1)

    monkeypatch.setattr("dbrel.ingest.replay.replay_day", fake_replay)
    root = tmp_path / "archive"
    root.mkdir()
    result = CliRunner().invoke(cli.app, [
        "backfill", "--from", "2026-05-10", "--to", "2026-05-10",
        "--root", str(root), "--dry-run", "--merge-plan-hours",
    ])
    assert result.exit_code == 0, result.output
    assert received == [True]
    engine.dispose()
    db.dispose_engine()
