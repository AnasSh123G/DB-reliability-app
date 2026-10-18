"""Backfill must protect its SQLite target and share its writer lock.

The database fixture and CLI helper are reused from ``test_backfill_resume`` so
these safety checks exercise the same isolated schema and replay stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_backfill_resume import _run, env


@pytest.mark.parametrize("collision", ["direct", "symlink", "hardlink", "wal", "shm"])
def test_done_file_cannot_replace_active_database_or_companion(env, collision):
    root, replayed, db = env
    before = db.read_bytes()
    if collision == "direct":
        done_file = db
    elif collision == "symlink":
        done_file = db.parent / "db-alias.sqlite"
        done_file.symlink_to(db)
    elif collision == "hardlink":
        done_file = db.parent / "db-hardlink.sqlite"
        done_file.hardlink_to(db)
    else:
        done_file = Path(f"{db}-{collision}")
        done_file.write_bytes(b"sqlite companion sentinel")

    result = _run(root, "--done-file", str(done_file))

    assert result.exit_code == 1, result.output
    assert replayed == [], "unsafe resume destination must be rejected before replay"
    assert db.read_bytes() == before
    if collision == "symlink":
        assert done_file.is_symlink()
        assert done_file.resolve() == db.resolve()
    if collision == "hardlink":
        assert done_file.read_bytes() == before
    if collision in {"wal", "shm"}:
        assert done_file.read_bytes() == b"sqlite companion sentinel"


def test_done_file_cannot_replace_default_database_when_active_db_is_custom(env, monkeypatch):
    from dbrel import config
    from dbrel import cli

    root, replayed, active_db = env
    default_dir = active_db.parent / "default-data"
    default_dir.mkdir()
    default_db = default_dir / "dbrel.sqlite"
    default_db.write_bytes(b"isolated default database sentinel")
    monkeypatch.setattr(config, "DATA_DIR", default_dir)
    monkeypatch.setattr(cli, "DB_PATH", active_db)
    before = default_db.read_bytes()

    result = _run(root, "--done-file", str(default_db))

    assert result.exit_code == 1, result.output
    assert replayed == []
    assert default_db.read_bytes() == before


def test_backfill_symlink_database_honors_canonical_writer_lock(env, monkeypatch):
    from dbrel import cli
    import dbrel.db as dbmod
    from dbrel.collect import exclusive_lock

    root, replayed, db = env
    alias = db.parent / "db-symlink.sqlite"
    alias.symlink_to(db)
    monkeypatch.setattr(dbmod, "DB_PATH", alias)
    monkeypatch.setattr(cli, "DB_PATH", alias)
    before = db.read_bytes()

    with exclusive_lock(Path(f"{db.resolve()}.writer.lock"), label="other writer"):
        result = _run(root)

    assert result.exit_code == 1, result.output
    assert "is held by" in result.output
    assert replayed == []
    assert db.read_bytes() == before


def test_backfill_honors_custom_database_writer_lock_held_by_collector(env):
    from dbrel.collect import exclusive_lock

    root, replayed, db = env
    before = db.read_bytes()
    with exclusive_lock(Path(f"{db.resolve()}.writer.lock"), label="collect"):
        result = _run(root)

    assert result.exit_code == 1, result.output
    assert "is held by" in result.output
    assert replayed == []
    assert db.read_bytes() == before


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_backfill_rejects_database_alias_as_writer_lock(env, alias_kind):
    import os

    root, replayed, db = env
    lock = Path(f"{db.resolve()}.writer.lock")
    if alias_kind == "symlink":
        lock.symlink_to(db)
    else:
        os.link(db, lock)
    before = db.read_bytes()

    result = _run(root)

    assert result.exit_code != 0, result.output
    assert replayed == [], "unsafe lock inode must be rejected before replay"
    assert db.read_bytes() == before, "opening the lock must not truncate the database"


@pytest.mark.parametrize("lock_kind", ["writer", "sweep"])
@pytest.mark.parametrize("alias_kind", ["direct", "symlink", "hardlink"])
def test_done_file_cannot_overwrite_lock_file_or_alias(
    env, tmp_path, monkeypatch, lock_kind, alias_kind
):
    import os

    from dbrel import cli
    from dbrel.collect import writer_lock_path

    root, replayed, db = env
    lock = writer_lock_path(db) if lock_kind == "writer" else tmp_path / "sweep.lock"
    if lock_kind == "sweep":
        monkeypatch.setattr(cli, "TT_SWEEP_LOCK", lock)
    lock.write_bytes(b"prior lock contents")

    if alias_kind == "direct":
        done_file = lock
    elif alias_kind == "symlink":
        done_file = tmp_path / f"{lock_kind}-lock-alias"
        done_file.symlink_to(lock)
    else:
        done_file = tmp_path / f"{lock_kind}-lock-hardlink"
        os.link(lock, done_file)

    before_lock = lock.read_bytes()
    before_done = done_file.read_bytes()
    before_db = db.read_bytes()
    result = _run(root, "--done-file", str(done_file))

    assert result.exit_code == 1, result.output
    assert replayed == [], "unsafe resume destination must be rejected before replay"
    assert lock.read_bytes() == before_lock
    assert done_file.read_bytes() == before_done
    assert db.read_bytes() == before_db
    if alias_kind == "symlink":
        assert done_file.is_symlink()


@pytest.mark.parametrize("contents", [b"[]", b'["2026-09-20"]'])
def test_collect_lock_contention_accepts_legacy_list_metadata(tmp_path, contents):
    import fcntl
    import os

    from dbrel.collect import AlreadyRunning, exclusive_lock

    lock = tmp_path / "legacy.lock"
    lock.write_bytes(contents)
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(AlreadyRunning):
            with exclusive_lock(lock, label="contender"):
                pytest.fail("contending process unexpectedly acquired the lock")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_collector_honors_custom_database_writer_lock_held_by_backfill(env, monkeypatch):
    from dbrel.collect import AlreadyRunning, run_collect
    import dbrel.db as dbmod

    _root, _replayed, db = env
    monkeypatch.setattr(dbmod, "DB_PATH", db)

    with pytest.raises(AlreadyRunning, match="is held by"):
        with exclusive_database_writer_lock(db):
            run_collect(
                every_s=1,
                for_s=1,
                max_lookback_s=60,
                stations_file=db.parent / "unused-stations.csv",
                cold_start=False,
                once=True,
            )


def exclusive_database_writer_lock(db: Path):
    """Use the per-database lock with the label a backfill owns."""
    from dbrel.collect import exclusive_lock

    return exclusive_lock(Path(f"{db.resolve()}.writer.lock"), label="backfill")
