"""The streamed full-year steps equal their in-memory counterparts on the same rows."""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from dbrel.ml.arrival import ConditionalHistogram, arrival_inputs, fit_input_prep
from dbrel.ml.checkpoint import CheckpointMismatch
from dbrel.ml.features import ARRIVAL_NUMERIC
from dbrel.ml.full_history import FullHistory, build_full_history
from dbrel.ml.full_year import (
    RowSet, evaluation_cutoff, evaluation_report, history_key, memory_check_path, prep_from_statistics,
    prepare, training_statistics,
)
from dbrel.ml.launch import LaunchError
from dbrel.ml.memcheck import gate
from dbrel.ml.split import TrainPruning, training_vocabulary

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


def test_train_pruning_uses_the_streamed_counts(history):
    stats = training_statistics(history, RowSet(tuple(TRAIN)))
    values, counts, _ = stats["categories"]["train"]
    kept = training_vocabulary(stats["categories"], stats["last_day"], TrainPruning(min_count=int(max(counts))))
    assert kept["train"] == sorted(v for v, c in zip(values, counts) if c == max(counts))


def test_prepared_inputs_match_arrival_inputs_and_are_reused_by_key(history, tmp_path):
    rows = RowSet(tuple(TRAIN))
    stats = training_statistics(history, rows)
    vocab = training_vocabulary(stats["categories"], stats["last_day"])
    prep = prep_from_statistics(stats, vocab, "robust")
    root = tmp_path / "prepared"
    first = prepare(history, rows, vocab, prep, root, "train")
    ds = history.load(TRAIN)
    cats, nums = arrival_inputs(ds.features, prep)
    assert np.array_equal(first.cats, cats) and np.array_equal(first.nums, nums)
    assert np.array_equal(first.y, ds.arr_min) and np.array_equal(first.column("stop_id"), ds.stop_id)

    inode = (root / "train" / "nums.bin").stat().st_ino
    again = prepare(history, rows, vocab, prep, root, "train")
    assert (again.dir / "nums.bin").stat().st_ino == inode  # reused, not rewritten

    smaller = dict(vocab, train=vocab["train"][:1])
    with pytest.raises(CheckpointMismatch, match="vocab"):
        prepare(history, rows, smaller, prep, root, "train")
    rebuilt = prepare(history, rows, smaller, prep, root, "train", replace=True)
    assert rebuilt.key["vocab"] != first.key["vocab"]


def test_histogram_on_prepared_inputs_equals_the_in_memory_histogram(history, tmp_path):
    rows = RowSet(tuple(TRAIN))
    stats = training_statistics(history, rows)
    vocab = training_vocabulary(stats["categories"], stats["last_day"])
    prepared = prepare(history, rows, vocab, prep_from_statistics(stats, vocab, "standard"), tmp_path, "train")
    ds = history.load(TRAIN)
    everything = np.ones(len(ds.arr_min), bool)
    ours = ConditionalHistogram(min_support=2, point_rule="median").fit(
        prepared.histogram_encoded(), np.asarray(prepared.y), everything)
    theirs = ConditionalHistogram(min_support=2, point_rule="median").fit(ds.features, ds.arr_min, everything)
    assert np.array_equal(ours.predict(prepared.histogram_encoded()).point, theirs.predict(ds.features).point)


SEARCH = ["env", "PYTHONPATH=src", "venv/bin/python", "scripts/search_full_year.py", "search", "--track", "large"]


def _check(history, work, track, peak, final_peak=None):
    work.mkdir(parents=True, exist_ok=True)
    memory_check_path(work, track).write_text(json.dumps({
        "track": track, "history_root": str(history.root), "history": history_key(history),
        "projected_peak": peak, "projected_final_peak": final_peak if final_peak is not None else peak}))


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


def test_evaluation_report_breaks_down_by_month_and_segment(history, tmp_path):
    rows = RowSet(tuple(DATES))
    stats = training_statistics(history, rows)
    vocab = training_vocabulary(stats["categories"], stats["last_day"])
    prepared = prepare(history, rows, vocab, prep_from_statistics(stats, vocab, "standard"), tmp_path, "all")
    report = evaluation_report(history, prepared, {"zero": np.zeros(len(prepared))})
    assert set(report["zero"]["by_month"]) == {"2026-07", "2026-08", "2026-09"}
    assert set(report["zero"]["by_segment"]) == {"FV", "RV"}
    assert report["zero"]["overall"]["most_common_share"] == 1.0
    with pytest.raises(ValueError, match="expected"):
        evaluation_report(history, prepared, {"short": np.zeros(3)})
