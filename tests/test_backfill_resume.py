"""The backfill resumes per day, and --dry-run writes nothing.

A 30-day national replay is long enough that being interrupted is the normal
case, not the exceptional one, so "continues where it stopped" has to be a
property rather than a hope. Both are exercised through the CLI, because the
resume point is the command's business -- replay_day itself is deliberately
stateless about which days have been done.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbrel import cli
from dbrel.ingest.replay import ReplayResult

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A schema, one tracked station, and a stubbed replay that records its calls.

    db.py caches the engine in a module singleton, so pointing the CLI at a
    temporary database means patching the path db.make_engine reads and disposing
    the cached engine either side of the test.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import dbrel.db as dbmod
    from dbrel.db import Base
    from dbrel.models import Station

    db = tmp_path / "dbrel.sqlite"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    monkeypatch.setattr(cli, "DB_PATH", db)
    dbmod.dispose_engine()

    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Station(
                natural_key="dormagen@51.099,6.837",
                name="Dormagen",
                lat=51.099,
                lon=6.837,
                eva=8001506,
                tracked=1,
            )
        )
        session.commit()
    engine.dispose()

    replayed: list[date] = []

    def fake_replay_day(session, store, day, *, tracked_evas, dry_run=False, merge_plan_hours=False, plan_writer="per_run", progress=None):
        replayed.append(day)
        result = ReplayResult(days=1, sweeps=1, stops_stored=10, plan_buckets=2)
        result.by_quality = {"NO_DATA": 3}
        result.gaps_written = 3
        return result

    monkeypatch.setattr("dbrel.ingest.replay.replay_day", fake_replay_day)
    root = tmp_path / "archive"
    root.mkdir()
    yield root, replayed, db
    dbmod.dispose_engine()


def _run(root: Path, *extra: str):
    return runner.invoke(
        cli.app,
        ["backfill", "--from", "2026-09-20", "--to", "2026-09-22", "--root", str(root), *extra],
    )


# --- task 4.2: resume ------------------------------------------------------


def test_a_finished_day_is_recorded_and_skipped_next_time(env):
    root, replayed, _ = env
    first = _run(root)
    assert first.exit_code == 0, first.output
    assert replayed == [date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)]

    done = json.loads((root / "replayed.json").read_text())
    assert done == ["2026-09-20", "2026-09-21", "2026-09-22"]

    replayed.clear()
    second = _run(root)
    assert second.exit_code == 0
    assert replayed == [], "every day was already done"
    assert "already replayed" in second.output


def test_an_interrupted_run_continues_rather_than_restarting(env):
    """Two of three days recorded: only the third may be replayed."""
    root, replayed, _ = env
    (root / "replayed.json").write_text(json.dumps(["2026-09-20", "2026-09-21"]))

    result = _run(root)
    assert result.exit_code == 0, result.output
    assert replayed == [date(2026, 9, 22)], "the finished days were not redone"


def test_restart_redoes_the_whole_window(env):
    root, replayed, _ = env
    (root / "replayed.json").write_text(json.dumps(["2026-09-20", "2026-09-21", "2026-09-22"]))

    result = _run(root, "--restart")
    assert result.exit_code == 0, result.output
    assert len(replayed) == 3


def test_a_corrupt_resume_file_replays_everything_rather_than_crashing(env):
    root, replayed, _ = env
    (root / "replayed.json").write_text("{not json")
    result = _run(root)
    assert result.exit_code == 0, result.output
    assert len(replayed) == 3


# --- task 3.6: dry run ----------------------------------------------------


def test_dry_run_records_no_resume_point(env):
    """Otherwise a dry run would convince the next real run there was nothing to do."""
    root, replayed, _ = env
    result = _run(root, "--dry-run")
    assert result.exit_code == 0, result.output
    assert len(replayed) == 3
    assert not (root / "replayed.json").exists()
    assert "nothing was written" in result.output


def test_dry_run_leaves_the_database_byte_identical(env):
    root, _replayed, db = env
    before = db.read_bytes()
    result = _run(root, "--dry-run")
    assert result.exit_code == 0, result.output
    assert db.read_bytes() == before, "--dry-run must not touch the database"


# --- reporting ------------------------------------------------------------


def test_the_grade_histogram_is_reported(env):
    """An operator has to see the NO_DATA share rather than infer it."""
    root, _replayed, _ = env
    result = _run(root)
    assert "NO_DATA" in result.output
    assert "gap row" in result.output


def test_an_installation_with_no_tracked_stations_is_told_so(env, monkeypatch):
    root, _replayed, db = env
    from sqlalchemy import create_engine, update
    from sqlalchemy.orm import Session

    from dbrel.models import Station

    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    with Session(engine) as session:
        session.execute(update(Station).values(tracked=0))
        session.commit()

    result = _run(root)
    assert result.exit_code == 1
    assert "no tracked stations" in result.output


def test_custom_done_file_isolated_from_archive_resume(env):
    root, replayed, _ = env
    custom = root.parent / 'resume' / 'scratch.json'
    first = _run(root, '--done-file', str(custom))
    assert first.exit_code == 0, first.output
    assert json.loads(custom.read_text()) == ['2026-09-20', '2026-09-21', '2026-09-22']
    assert not (root / 'replayed.json').exists()
    replayed.clear()
    assert _run(root, '--done-file', str(custom)).exit_code == 0
    assert replayed == []


def test_backfill_lock_conflict_writes_nothing(env):
    from dbrel.collect import exclusive_lock
    root, replayed, db = env
    before = db.read_bytes()
    with exclusive_lock(Path(str(db) + '.writer.lock'), label='other backfill'):
        result = _run(root)
    assert result.exit_code == 1
    assert 'is held by' in result.output
    assert replayed == []
    assert db.read_bytes() == before
    assert not (root / 'replayed.json').exists()


def test_default_database_also_honors_collect_lock(env, monkeypatch):
    from dbrel import config
    from dbrel.collect import exclusive_lock
    root, replayed, db = env
    monkeypatch.setattr(config, 'DATA_DIR', db.parent)
    monkeypatch.setattr(cli, 'TT_SWEEP_LOCK', db.parent / 'sweep.lock')
    with exclusive_lock(cli.TT_SWEEP_LOCK):
        result = _run(root)
    assert result.exit_code == 1
    assert 'is held by' in result.output
    assert replayed == []


def test_timing_output_is_opt_in(env):
    root, _, _ = env
    result = _run(root, '--timings')
    assert result.exit_code == 0, result.output
    assert 'timings 2026-09-20 plan_s=' in result.output
    assert 'upsert_calls=' in result.output
    assert 'refs_refreshed=' in result.output
