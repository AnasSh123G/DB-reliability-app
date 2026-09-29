"""The monthly-shard full-history dataset: same rows as the single-file builder, rebuilt per month."""

from __future__ import annotations

import shutil

import numpy as np
import pytest
from sqlalchemy.orm import Session

from dbrel.db import make_engine
from dbrel.ml import features as F
from dbrel.ml.dataset import build_arrival_dataset, load_arrival_dataset
from dbrel.ml.full_history import FullHistory, StaleHistory, build_full_history, month_bounds

from ml_fixture import add_run, build

# Three calendar months, so "only the changed month" is distinguishable from "the latest month".
DATES = ["2026-07-30", "2026-07-31", "2026-08-01", "2026-08-02",
         "2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02"]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "dbrel.sqlite"
    build(path, DATES)
    return path


def _directions(db):
    engine = make_engine(db)
    with Session(engine) as s:
        out = F.all_directions(s)
    engine.dispose()
    return out


def _files(root):
    return {p.relative_to(root): (p.stat().st_ino, p.read_bytes()) for p in sorted(root.glob("*/*.bin"))}


def test_month_bounds_cross_the_year():
    assert month_bounds("2025-12") == ("2025-12-01", "2026-01-01")
    assert month_bounds("2026-02") == ("2026-02-01", "2026-03-01")


def test_shards_hold_what_the_single_file_builder_holds(db, tmp_path):
    dirs = _directions(db)
    build_arrival_dataset(db, tmp_path / "ml", dirs)
    old = load_arrival_dataset(tmp_path / "ml")
    manifest = build_full_history(db, tmp_path / "full", dirs)
    new = FullHistory(tmp_path / "full").load()

    assert list(manifest["shards"]) == ["2026-07", "2026-08", "2026-09"]
    assert manifest["n_rows"] == len(old.arr_min) == len(new.arr_min)
    assert new.meta["rows_per_date"] == old.meta["rows_per_date"]
    assert new.meta["vocab"] == old.meta["vocab"]
    a, b = np.argsort(old.stop_id), np.argsort(new.stop_id)
    assert np.array_equal(old.stop_id[a], new.stop_id[b])
    for name in F.FEATURES + F.SEASONAL:
        np.testing.assert_array_equal(old.features[name][a], new.features[name][b], err_msg=name)
    for field in ("arr_min", "trip_id", "stop_sequence", "scheduled_arrival_utc", "cutoff_utc",
                  "last_observed_at", "finalized_at", "v2_sched_local_sec", "segment"):
        np.testing.assert_array_equal(getattr(old, field)[a], getattr(new, field)[b], err_msg=field)
    dates_old = np.array(old.meta["dates"])[old.day[a]]
    assert np.array_equal(dates_old, np.array(new.meta["dates"])[new.day[b]])
    assert "" in new.meta["vocab"]["state"]  # Berlin's unresolved state survives the int16 codes


def test_rebuild_redoes_only_changed_months_and_the_latest(db, tmp_path):
    dirs, root = _directions(db), tmp_path / "full"
    first = build_full_history(db, root, dirs)
    assert first["rebuilt"] == ["2026-07", "2026-08", "2026-09"]
    before = _files(root)

    add_run(db, 70, "2026-07-29")
    second = build_full_history(db, root, dirs)
    after = _files(root)
    assert second["rebuilt"] == ["2026-07", "2026-09"]
    assert second["shards"]["2026-07"]["rows"] == first["shards"]["2026-07"]["rows"] + 2  # two arrivals
    for path, (inode, data) in before.items():
        if path.parts[0] == "2026-08":
            assert after[path] == (inode, data), path  # never rewritten
        elif path.parts[0] == "2026-09":
            assert after[path][1] == data, path  # rewritten, same contents
    assert after[next(p for p in after if p.parts[0] == "2026-07" and p.name == "arr_min.bin")][1] != \
        before[next(p for p in before if p.parts[0] == "2026-07" and p.name == "arr_min.bin")][1]

    assert build_full_history(db, root, dirs)["rebuilt"] == ["2026-09"]
    assert build_full_history(db, root, dirs, full=True)["rebuilt"] == ["2026-07", "2026-08", "2026-09"]


def test_loading_one_month_never_opens_the_others(db, tmp_path):
    root = tmp_path / "full"
    manifest = build_full_history(db, root, _directions(db))
    history = FullHistory(root)
    shutil.rmtree(root / "2026-07")
    shutil.rmtree(root / "2026-09")
    ds = history.load(["2026-08-01", "2026-08-31"])
    assert ds.meta["dates"] == ["2026-08-01", "2026-08-31"]
    rows = manifest["shards"]["2026-08"]["rows_per_date"]
    assert len(ds.arr_min) == rows["2026-08-01"] + rows["2026-08-31"]
    with pytest.raises(StaleHistory):
        history.load(["2026-07-30"])


def test_a_truncated_column_is_refused(db, tmp_path):
    root = tmp_path / "full"
    build_full_history(db, root, _directions(db))
    path = root / "2026-08" / "arr_min.bin"
    path.write_bytes(path.read_bytes()[:-4])
    with pytest.raises(StaleHistory, match="rebuild"):
        next(FullHistory(root).chunks(["2026-08-01"]))


def test_cli_prints_per_month_counts_matching_sql(db, tmp_path, monkeypatch):
    import sqlite3

    from typer.testing import CliRunner

    import dbrel.config as config
    import dbrel.db as dbmod
    from dbrel.cli import app
    from dbrel.ml.dataset import _query

    monkeypatch.setattr(dbmod, "DB_PATH", db)
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "ML_DATA_DIR", tmp_path / "ml")
    dbmod.dispose_engine()
    try:
        result = CliRunner().invoke(app, ["ml", "build-full-dataset"])
    finally:
        dbmod.dispose_engine()
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(db)
    conn.execute("CREATE TEMP TABLE ml_stop_rank(id INTEGER PRIMARY KEY, stop_rank, n_stops)")
    for month in ("2026-07", "2026-08", "2026-09"):
        (n,) = conn.execute(f"SELECT COUNT(*) FROM ({_query(arrival_only=True, by_date_range=True)})",
                            month_bounds(month)).fetchone()
        assert f"* {month}  {n:>12,} rows" in result.output
    assert (tmp_path / "ml" / "arrival_full" / "manifest.json").exists()


def test_chunks_select_dates_across_chunk_boundaries(db, tmp_path):
    root = tmp_path / "full"
    build_full_history(db, root, _directions(db))
    history = FullHistory(root)
    wanted = ["2026-07-31", "2026-09-02"]
    got = np.concatenate([c["stop_id"] for c in history.chunks(wanted, ["stop_id"], chunk_rows=7)])
    assert len(got) == sum(history.rows_per_date[d] for d in wanted)
    assert np.array_equal(np.sort(got), np.sort(history.load(wanted).stop_id))
