"""Building the deployed database from a small collection database."""

from __future__ import annotations

import hashlib
import sqlite3

import ml_fixture
import pytest

from dbrel import deployed
from dbrel.db import make_engine
from dbrel.deployed import UNREAD_TRAIN_COLUMNS
from dbrel.deployed_build import DeployedBuildError, build_deployed
from dbrel.ml.features import NOTICE_RATES
from dbrel.ml.notice_rates import NoticeRates

COLUMNS = ("id, feed_version_id, train_id, path_sha1, stop_sequence, station_id, source_stop_id, platform_code, "
           "planned_arrival_sec, planned_departure_sec, pickup_type, drop_off_type")


@pytest.fixture(scope="module")
def collection(tmp_path_factory):
    """A file-backed collection database built from the ML fixture."""
    path = tmp_path_factory.mktemp("collection") / "dbrel.sqlite"
    ml_fixture.build(path)
    ml_fixture.add_station_alias(path)
    return path


@pytest.fixture(scope="module")
def manifest(collection):
    """A model manifest whose notice-rate record matches the fixture's counts."""
    record = NoticeRates.from_database(make_engine(collection), ml_fixture.DATES).record()
    return {"prep": {"features_numeric": list(NOTICE_RATES)}, "notice_rates": record}


def _query(path, sql):
    """All rows of `sql` on the database at `path`, opened read-only."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _schedule(path):
    """Every station_stop row of the database at `path`, in a fixed order."""
    return _query(path, f"SELECT {COLUMNS} FROM station_stop ORDER BY feed_version_id, train_id, path_sha1, "
                        "stop_sequence")


def _content_hash(path):
    """A hash of the full SQL dump of the database at `path`."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()
    finally:
        con.close()


def test_the_view_gives_back_every_schedule_row_and_the_source_is_untouched(collection, manifest, tmp_path):
    """Every schedule row comes back exactly and the collection database is unchanged."""
    before = _content_hash(collection)
    out, lines = tmp_path / "deployed.sqlite", []
    counts = build_deployed(collection, out, manifest=manifest, log=lines.append)
    rows = _schedule(out)
    assert rows == _schedule(collection) and rows
    per_feed = "SELECT feed_version_id, COUNT(*) FROM station_stop GROUP BY 1"
    assert _query(out, per_feed) == _query(collection, per_feed)
    assert counts["schedule_rows"] == len(rows)
    assert counts["patterns"] <= counts["variants"]
    assert _query(out, "SELECT COUNT(*) FROM sqlite_stat1")[0][0] > 0  # ANALYZE ran
    assert _content_hash(collection) == before
    assert not (out.parent / "deployed.sqlite.partial").exists()


def test_no_per_run_data_is_copied(collection, manifest, tmp_path):
    """Run, message and poll tables are absent; the views and summary are present."""
    out = tmp_path / "deployed.sqlite"
    build_deployed(collection, out, manifest=manifest, log=lambda m: None)
    tables = {name for (name,) in _query(out, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not tables & {"trip", "trip_stop", "tt_message", "tt_poll", "tt_plan_fetch", "notice_rate_daily"}
    assert {"variant_run_data", "notice_rate_total", "pattern_stop", "train_data", "variant_path"} <= tables
    assert "stop_pattern" not in tables
    views = {name for (name,) in _query(out, "SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert views == {"station_stop", "train", "train_variant_run", "train_variant_weekday"}
    runs = "SELECT train_id, path_sha1, MAX(service_date), COUNT(*) FROM trip GROUP BY 1, 2 ORDER BY 1, 2"
    assert _query(out, "SELECT * FROM train_variant_run ORDER BY 1, 2") == _query(collection, runs)


def test_the_train_view_gives_back_every_column_the_server_reads(collection, manifest, tmp_path):
    """The train view equals the source in every read column; the unread ones are empty."""
    out = tmp_path / "deployed.sqlite"
    build_deployed(collection, out, manifest=manifest, log=lambda m: None)
    names = [c for (c,) in _query(collection, "SELECT name FROM pragma_table_info('train')")]
    read = ", ".join(f'"{c}"' for c in names if c not in UNREAD_TRAIN_COLUMNS)
    rows = _query(out, f"SELECT {read} FROM train ORDER BY id")
    assert rows == _query(collection, f"SELECT {read} FROM train ORDER BY id") and rows
    assert {"route_path", "path_sha1", "origin_name"} <= set(names)
    unread = " OR ".join(f'"{c}" IS NOT NULL' for c in UNREAD_TRAIN_COLUMNS)
    assert _query(out, f"SELECT COUNT(*) FROM train WHERE {unread}") == [(0,)]


@pytest.fixture(scope="module")
def with_retired(collection, tmp_path_factory):
    """The fixture with ICE 12 retired (last seen 2026-08-01, newest 2026-09-10) and aliases for 12 and 70."""
    path = tmp_path_factory.mktemp("retired") / "dbrel.sqlite"
    con = sqlite3.connect(collection)
    con.execute(f"VACUUM INTO '{path}'")
    con.close()
    con = sqlite3.connect(path)
    con.execute("UPDATE train SET last_seen_date = '2026-08-01' WHERE id = 12")
    con.execute("UPDATE train SET last_seen_date = '2026-09-10' WHERE id = 70")
    con.execute("INSERT INTO search_alias (alias_norm, alias_display, kind, weight, train_id) VALUES "
                "('ice12', 'ICE 12', 'CAT_NUMBER', 100, 12), ('re70', 'RE 70', 'CAT_NUMBER', 100, 70)")
    con.commit()
    con.close()
    return path


def test_a_retired_train_keeps_only_its_train_row(with_retired, tmp_path):
    """A retired train has its train row but no schedule, run-summary or alias rows; others keep theirs."""
    out = tmp_path / "deployed.sqlite"
    counts = build_deployed(with_retired, out, log=lambda m: None)
    assert counts["retired_trains"] == 1
    assert _query(out, "SELECT id FROM train WHERE id = 12") == [(12,)]
    for table in ("station_stop", "train_variant_run", "search_alias"):
        assert _query(out, f"SELECT COUNT(*) FROM {table} WHERE train_id = 12") == [(0,)], table
        assert _query(out, f"SELECT COUNT(*) FROM {table} WHERE train_id = 70")[0][0] > 0, table
    running = "SELECT * FROM station_stop WHERE train_id <> 12 ORDER BY feed_version_id, train_id, path_sha1, stop_sequence"
    assert _query(out, running.replace("SELECT *", f"SELECT {COLUMNS}")) == _query(
        with_retired, running.replace("SELECT *", f"SELECT {COLUMNS}"))


def test_the_build_reports_size_and_counts(collection, manifest, tmp_path):
    """The last log line states size, rows, patterns and summary rows."""
    lines = []
    build_deployed(collection, tmp_path / "deployed.sqlite", manifest=manifest, log=lines.append)
    report = lines[-1]
    for words in ("MiB", "schedule rows", "patterns", "pattern stops", "run-summary rows"):
        assert words in report


def test_a_rebuilt_row_that_differs_is_refused_and_the_old_file_kept(collection, manifest, tmp_path, monkeypatch):
    """A wrong pattern stop fails the build, is named, and leaves the previous output."""
    out = tmp_path / "deployed.sqlite"
    out.write_bytes(b"the previous build")
    real = deployed.encode_variant

    def wrong_platform_at_dortmund(rows):
        """`encode_variant`, but with platform X at station 2 (a deliberate bug)."""
        start, pattern, runs = real(rows)
        pattern = tuple(stop[:3] + ("X",) + stop[4:] if stop[1] == 2 else stop for stop in pattern)
        return start, pattern, runs

    monkeypatch.setattr(deployed, "encode_variant", wrong_platform_at_dortmund)
    with pytest.raises(DeployedBuildError, match=r"schedule row differs at feed_version_id \d+, train_id \d+, path_sha1 'main', stop_sequence 20: platform_code"):
        build_deployed(collection, out, manifest=manifest, log=lambda m: None)
    assert out.read_bytes() == b"the previous build"
    assert not (tmp_path / "deployed.sqlite.partial").exists()


def test_changed_notice_counts_are_refused(collection, manifest, tmp_path):
    """A manifest fingerprint that does not match the counts fails the build."""
    stale = {**manifest, "notice_rates": {**manifest["notice_rates"], "fingerprint": "0" * 64}}
    with pytest.raises(Exception, match="differ from the ones this model was trained with"):
        build_deployed(collection, tmp_path / "deployed.sqlite", manifest=stale, log=lambda m: None)
    assert not (tmp_path / "deployed.sqlite").exists()


def test_a_source_without_station_aliases_is_refused(tmp_path):
    """Stations without any station search alias fail the build, naming the command that fixes it."""
    source = tmp_path / "dbrel.sqlite"
    ml_fixture.build(source)
    with pytest.raises(DeployedBuildError, match="no station search aliases.*dbrel build-aliases"):
        build_deployed(source, tmp_path / "deployed.sqlite", log=lambda m: None)
    assert not (tmp_path / "deployed.sqlite").exists()


def test_running_profile_matches_collection_and_reports_window(collection, tmp_path):
    """The stored weekdays and window reproduce the collection loader, including the observation window."""
    from sqlalchemy.orm import Session
    from dbrel.planner.profile import load_run_profile

    out, lines = tmp_path / "deployed.sqlite", []
    counts = build_deployed(collection, out, log=lines.append)
    with Session(make_engine(collection)) as source, Session(make_engine(out)) as target:
        expected, actual = load_run_profile(source), load_run_profile(target)
    assert actual == expected
    assert actual.counts
    rows = _query(out, "SELECT COUNT(*) FROM train_variant_weekday")[0][0]
    assert rows == counts["running_profile"] > 0
    assert _query(out, "SELECT * FROM running_profile_window") == [
        (expected.first_date.isoformat(), expected.last_date.isoformat())]
    assert f"running profile: {rows} rows, window {expected.first_date}..{expected.last_date}" in lines


def test_retired_train_has_no_running_profile(with_retired, tmp_path):
    """Retired trains do not retain weekday counts."""
    out = tmp_path / "deployed.sqlite"
    build_deployed(with_retired, out, log=lambda m: None)
    assert _query(out, "SELECT * FROM variant_weekday_data WHERE train_id = 12") == []
    assert _query(out, "SELECT COUNT(*) FROM variant_weekday_data WHERE train_id = 70")[0][0] > 0


def test_running_profile_mismatch_keeps_previous_output(collection, tmp_path, monkeypatch):
    """A source verification mismatch identifies the train and variant without replacing output."""
    import dbrel.deployed_build as builder

    out = tmp_path / "deployed.sqlite"
    out.write_bytes(b"previous deployed database")
    real_sql = builder.PROFILE_SQL

    class MismatchingProfileSQL(str):
        def format(self, **kwargs):
            sql = real_sql.format(**kwargs)
            if kwargs["trip"] == "trip":
                return "SELECT train_id, path_sha1, weekday, n_runs + 1 AS n_runs FROM (" + sql + ")"
            return sql

    monkeypatch.setattr(builder, "PROFILE_SQL", MismatchingProfileSQL(real_sql))
    with pytest.raises(DeployedBuildError, match=r"running profile row differs at train_id .*path_sha1 'main'.*weekday"):
        build_deployed(collection, out, log=lambda m: None)
    assert out.read_bytes() == b"previous deployed database"
    assert not (tmp_path / "deployed.sqlite.partial").exists()


def test_running_profile_keeps_null_variant_hash(collection, tmp_path):
    """NULL variants survive the lookup-table join through path id zero."""
    source, out = tmp_path / "source.sqlite", tmp_path / "deployed.sqlite"
    with sqlite3.connect(collection) as conn:
        conn.execute(f"VACUUM INTO '{source}'")
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE trip SET path_sha1 = NULL WHERE train_id = 70")
    build_deployed(source, out, log=lambda m: None)
    rows = _query(out, "SELECT path_sha1, weekday, n_runs FROM train_variant_weekday WHERE train_id = 70 ORDER BY weekday")
    assert rows
    assert all(path is None for path, _, _ in rows)
    assert _query(out, "SELECT DISTINCT path_id FROM variant_weekday_data WHERE train_id = 70") == [(0,)]


def test_empty_trip_history_has_an_empty_running_profile(collection, tmp_path):
    """An empty source has no dates to store, and the deployed loader returns an empty profile."""
    from sqlalchemy.orm import Session
    from dbrel.planner.profile import RunProfile, load_run_profile

    source, out = tmp_path / "source.sqlite", tmp_path / "deployed.sqlite"
    with sqlite3.connect(collection) as conn:
        conn.execute(f"VACUUM INTO '{source}'")
    with sqlite3.connect(source) as conn:
        conn.execute("DELETE FROM trip")
    counts = build_deployed(source, out, log=lambda m: None)
    with Session(make_engine(out)) as session:
        assert load_run_profile(session) == RunProfile({}, None, None)
    assert counts["running_profile"] == 0
    assert _query(out, "SELECT * FROM running_profile_window") == []
