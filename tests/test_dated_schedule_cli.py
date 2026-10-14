"""Safe migration activation and readable failures on an unmigrated collection."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from dbrel import cli, db
from dbrel.db import Base


def test_migration_cli_creates_only_snapshot_table_and_is_idempotent(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables
                                           if t.name != "trip_schedule_snapshot"])
    before = set(inspect(engine).get_table_names())
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    runner = CliRunner()
    first = runner.invoke(cli.app, ["migrate-trip-schedule-schema"])
    second = runner.invoke(cli.app, ["migrate-trip-schedule-schema"])
    assert first.exit_code == second.exit_code == 0
    assert "created" in first.stdout and "already present" in second.stdout
    assert set(inspect(engine).get_table_names()) == before | {"trip_schedule_snapshot"}
    engine.dispose()


def test_existing_incompatible_snapshot_table_is_rejected():
    from dbrel.ingest.schedule_schema import ensure_dated_schedule_schema, require_dated_schedule_schema
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables
                                           if t.name != "trip_schedule_snapshot"])
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE trip_schedule_snapshot (id INTEGER PRIMARY KEY)"))
    with pytest.raises(ValueError, match="incompatible"):
        ensure_dated_schedule_schema(engine)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="incompatible"):
            require_dated_schedule_schema(session)
    assert [c['name'] for c in inspect(engine).get_columns('trip_schedule_snapshot')] == ['id']
    engine.dispose()


def test_ingestion_schema_guard_gives_migration_command_without_creating_tables():
    from dbrel.ingest.schedule_schema import require_dated_schedule_schema

    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as session:
        with pytest.raises(ValueError, match="migrate-trip-schedule-schema"):
            require_dated_schedule_schema(session)
    assert inspect(engine).get_table_names() == []
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        require_dated_schedule_schema(session)
    engine.dispose()
