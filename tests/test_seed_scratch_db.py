"""Exercise the seed CLI against tiny databases, never the live database."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/seed_scratch_db.py"


def run_seed(source, target, **kwargs):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--live", str(source), "--out", str(target)],
        text=True, capture_output=True, **kwargs,
    )


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE feed_version (id INTEGER PRIMARY KEY, kind TEXT, source_url TEXT,
              content_sha256 TEXT, downloaded_at INTEGER, status TEXT, old_column TEXT);
            INSERT INTO feed_version VALUES (7,'tt','https://example.test','abc',42,'ready','old');
            CREATE TABLE station (id INTEGER PRIMARY KEY, natural_key TEXT, name TEXT,
              lat REAL, lon REAL, first_seen_feed_version_id INTEGER, tracked INTEGER,
              poll_priority INTEGER, last_plan_hour TEXT, last_fchg_at INTEGER,
              n_fchg_polls INTEGER, n_fchg_errors INTEGER);
            INSERT INTO station VALUES (11,'alpha','Alpha',50,8,7,1,3,'2026-06-09T01',123,8,2);
            INSERT INTO station VALUES (12,'beta','Beta',51,9,7,0,2,NULL,NULL,0,0);
            CREATE TABLE route (id INTEGER PRIMARY KEY, feed_version_id INTEGER,
              gtfs_route_id TEXT, route_type INTEGER);
            INSERT INTO route VALUES (19,7,'R1',2);
            CREATE TABLE public_holiday (date TEXT, state TEXT, name TEXT, partial INTEGER);
            INSERT INTO public_holiday VALUES ('2026-01-01','DE-BE','Neujahr',0);
            CREATE TABLE trip (id INTEGER PRIMARY KEY);
            INSERT INTO trip VALUES (99);
        ''')
    return path


def test_seed_copies_only_reference_rows_and_resets_ingest_state(source, tmp_path):
    target = tmp_path / "seed.sqlite"
    before = source.read_bytes()
    # A stale environment must not choose the output database.
    result = run_seed(source, target, env={**os.environ, "DBREL_DB": str(source)})
    assert result.returncode == 0, result.stderr
    assert f"DBREL_DB={target}" in result.stdout
    for table, count in (("station", 2), ("feed_version", 1), ("route", 1), ("public_holiday", 1)):
        assert f"{table}: {count}" in result.stdout
    assert source.read_bytes() == before
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT id,tracked,poll_priority,last_plan_hour,last_fchg_at,n_fchg_polls,n_fchg_errors FROM station ORDER BY id').fetchall() == [
            (11,1,3,None,None,0,0), (12,0,2,None,None,0,0)]
        assert conn.execute('SELECT id,first_seen_feed_version_id FROM station ORDER BY id').fetchall() == [(11,7),(12,7)]
        assert conn.execute('SELECT id,feed_version_id FROM route').fetchall() == [(19,7)]
        assert conn.execute('SELECT count(*) FROM trip').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM trip_schedule_snapshot').fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='view'").fetchone()[0] > 0
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('alias', ['same', 'symlink', 'hardlink', 'existing'])
def test_seed_refuses_existing_or_alias_target(source, tmp_path, alias):
    target = tmp_path / 'target.sqlite'
    if alias == 'same':
        target = source
    elif alias == 'symlink':
        target.symlink_to(source)
    elif alias == 'hardlink':
        os.link(source, target)
    else:
        target.write_bytes(b'keep me')
    before = target.read_bytes()
    result = run_seed(source, target)
    assert result.returncode != 0
    assert target.read_bytes() == before


def test_seed_refuses_default_live_path_even_if_absent(source, tmp_path):
    (tmp_path / 'data').mkdir()
    target = tmp_path / 'data/dbrel.sqlite'
    result = run_seed(source, target, cwd=tmp_path)
    assert result.returncode != 0
    assert not target.exists()


def test_seed_missing_source_does_not_create_output(tmp_path):
    target = tmp_path / 'seed.sqlite'
    result = run_seed(tmp_path / 'missing.sqlite', target)
    assert result.returncode != 0
    assert not target.exists()


def test_seed_incompatible_source_removes_partial_output(tmp_path):
    source = tmp_path / 'empty.sqlite'
    with sqlite3.connect(source):
        pass
    target = tmp_path / 'seed.sqlite'
    result = run_seed(source, target)
    assert result.returncode == 2
    assert 'Source reference table missing or incompatible' in result.stderr
    assert not target.exists()
    assert not Path(str(target) + '-wal').exists()
    assert not Path(str(target) + '-shm').exists()


def test_seed_source_uri_escapes_special_characters(source, tmp_path):
    unusual = tmp_path / 'source ?# database.sqlite'
    source.rename(unusual)
    target = tmp_path / 'seed.sqlite'
    result = run_seed(unusual, target)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT count(*) FROM station').fetchone() == (2,)
