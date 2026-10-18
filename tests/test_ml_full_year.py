"""The streamed full-year steps equal their in-memory counterparts on the same rows."""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from dbrel.ml.arrival import fit_input_prep
from dbrel.ml.features import ARRIVAL_NUMERIC
from dbrel.ml.full_history import FullHistory, build_full_history
from dbrel.ml.full_year import (
    RowSet, evaluation_cutoff, history_key, memory_check_path, prep_from_statistics,
    training_statistics,
)
from dbrel.ml.launch import LaunchError
from dbrel.ml.memcheck import gate
from dbrel.ml.split import training_vocabulary

from test_ml_full_history import DATES, _directions, db  # noqa: F401 - fixture

TRAIN = DATES[:6]
VALIDATION = DATES[6:]


@pytest.fixture
def history(db, tmp_path):  # noqa: F811
    build_full_history(db, tmp_path / "full", _directions(db))
    return FullHistory(tmp_path / "full")


def _last_day(dates):
    return int(np.datetime64(max(dates), "D").astype(np.int64))


@pytest.mark.parametrize("scaling", ["standard", "robust"])
def test_streamed_vocabulary_and_prep_equal_the_in_memory_fit(history, scaling):
    rows = RowSet(tuple(TRAIN))
    stats = training_statistics(history, rows, sample_rows=10_000)
    assert stats["sample_share"] == 1.0 and stats["last_day"] == _last_day(TRAIN)
    vocab = training_vocabulary(stats["categories"], stats["last_day"])
    ds = history.load(TRAIN)
    assert vocab == ds.meta["vocab"]
    streamed = prep_from_statistics(stats, vocab, scaling)
    in_memory = fit_input_prep(ds.features, np.ones(len(ds.arr_min), bool), vocab, scaling, ARRIVAL_NUMERIC)
    assert streamed["numeric"] == in_memory["numeric"] and streamed["cards"] == in_memory["cards"]
    assert streamed["features_numeric"] == list(ARRIVAL_NUMERIC)
    for name, (centre, scale) in in_memory["stats"].items():
        assert streamed["stats"][name] == pytest.approx([centre, scale], rel=1e-9, abs=1e-9), name


def test_training_rows_end_at_the_evaluation_cutoff(db, tmp_path):  # noqa: F811
    conn = sqlite3.connect(db)
    conn.execute("UPDATE trip SET finalized_at = CAST(strftime('%s', service_date) AS INTEGER) + 2 * 86400")
    conn.commit()
    conn.close()
    build_full_history(db, tmp_path / "full", _directions(db))
    history = FullHistory(tmp_path / "full")
    cutoff = evaluation_cutoff(history, VALIDATION)
    ds = history.load(VALIDATION)
    assert cutoff == int(ds.scheduled_arrival_utc.min()) - 5 * 3600
    kept = history.load(TRAIN)
    expected = int(np.count_nonzero((kept.finalized_at > 0) & (kept.finalized_at <= cutoff)))
    assert 0 < expected < len(kept.arr_min)  # the runs finalized after the cutoff are dropped
    assert training_statistics(history, RowSet(tuple(TRAIN), cutoff))["rows"] == expected


SEARCH = ["env", "PYTHONPATH=src", "venv/bin/python", "scripts/search_full_year.py", "search", "--track", "large"]


def _check(history, work, track, peak, final_peak=None):
    work.mkdir(parents=True, exist_ok=True)
    memory_check_path(work, track).write_text(json.dumps({
        "track": track, "history_root": str(history.root), "history": history_key(history),
        "notice_rates_bytes": {"search": 1, "final": 1}, "projected_peak": peak, "projected_final_peak": final_peak if final_peak is not None else peak}))


def test_launch_gate_needs_a_current_fitting_memory_check(history, tmp_path):
    ml = tmp_path / "ml"
    work = ml / "full_year"
    with pytest.raises(LaunchError, match="no memory check for the full-year large track"):
        gate([SEARCH], "14G", ml)
    _check(history, work, "large", 8 * 2**30)
    gate([SEARCH], "14G", ml)  # 8 GiB * 1.15 fits
    with pytest.raises(LaunchError, match=r"large: projected peak 8.0 GiB .* above the cap 8G"):
        gate([SEARCH], "8G", ml)  # a deliberately low cap refuses
    _check(history, work, "large", None)
    with pytest.raises(LaunchError, match="hit the cap on its own"):
        gate([SEARCH], "14G", ml)

    final = SEARCH[:4] + ["final", f"--work={work}"]
    _check(history, work, "large", 8 * 2**30, final_peak=13 * 2**30)
    with pytest.raises(LaunchError, match="no memory check for the full-year compact track"):
        gate([final], "14G", ml)
    _check(history, work, "compact", 4 * 2**30)
    with pytest.raises(LaunchError, match="large: projected peak 13.0 GiB"):
        gate([final], "14G", ml)  # the refit's larger projection is the one checked
    gate([final], "16G", ml)

    stale = json.loads(memory_check_path(work, "compact").read_text())
    stale["history"]["shards"] = {}
    memory_check_path(work, "compact").write_text(json.dumps(stale))
    with pytest.raises(LaunchError, match="different full-history build"):
        gate([final], "16G", ml)
    gate([SEARCH[:4] + ["memory-check", "--track", "large"]], "1G", ml)  # the probe itself is not gated


# -- notice-rate inputs (add-notice-inputs) -----------------------------------------------------------

def _rates(db, dates=TRAIN):  # noqa: F811
    from dbrel.db import make_engine
    from dbrel.ml.notice_rates import NoticeRates

    engine = make_engine(db)
    try:
        return NoticeRates.from_database(engine, list(dates))
    finally:
        engine.dispose()


def _execute(db, sql):  # noqa: F811
    conn = sqlite3.connect(db)
    conn.execute(sql)
    conn.commit()
    conn.close()


def _train_chunk(history):
    parts = list(history.chunks(TRAIN))
    return {name: np.concatenate([p[name] for p in parts]) for name in parts[0]}


def test_a_training_rows_rates_leave_its_own_month_out_and_other_rows_do_not(history, db):  # noqa: F811
    from dbrel.ml.features import NOTICE_RATES

    rates = _rates(db)
    chunk = _train_chunk(history)
    vocab = training_vocabulary(training_statistics(history, RowSet(tuple(TRAIN)))["categories"], 0)
    folded = history.encode(chunk, vocab, rates, True)
    plain = history.encode(chunk, vocab, rates, False)
    assert any(not np.array_equal(folded[n], plain[n], equal_nan=True) for n in NOTICE_RATES)
    assert all(np.array_equal(folded[n], plain[n], equal_nan=True) for n in ARRIVAL_NUMERIC)


