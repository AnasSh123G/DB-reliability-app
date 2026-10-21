"""Building the deployed database from a small collection database."""

from __future__ import annotations

import hashlib
import sqlite3

import ml_fixture
import pytest

from dbrel import deployed
from dbrel.db import make_engine
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


