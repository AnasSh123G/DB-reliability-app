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


@pytest.mark.parametrize("scaling", ["standard", "robust"])
def test_streamed_prep_with_notice_rates_equals_the_in_memory_fit(history, db, scaling):  # noqa: F811
    from dbrel.ml.features import NOTICE_NUMERIC, NOTICE_RATES

    rates = _rates(db)
    stats = training_statistics(history, RowSet(tuple(TRAIN)), numeric=NOTICE_NUMERIC, rates=rates, fold=True,
                                sample_rows=10_000)
    vocab = training_vocabulary(stats["categories"], stats["last_day"])
    streamed = prep_from_statistics(stats, vocab, scaling, NOTICE_NUMERIC)
    encoded = history.encode(_train_chunk(history), vocab, rates, True)
    in_memory = fit_input_prep(encoded, np.ones(len(encoded["is_holiday"]), bool), vocab, scaling, NOTICE_NUMERIC)
    assert streamed["numeric"] == in_memory["numeric"]
    assert streamed["features_numeric"] == list(NOTICE_NUMERIC)
    for name in NOTICE_RATES:
        assert name in streamed["numeric"]
        assert streamed["stats"][name] == pytest.approx(in_memory["stats"][name], rel=1e-6, abs=1e-9), name


def test_a_training_rows_rates_leave_its_own_month_out_and_other_rows_do_not(history, db):  # noqa: F811
    from dbrel.ml.features import NOTICE_RATES

    rates = _rates(db)
    chunk = _train_chunk(history)
    vocab = training_vocabulary(training_statistics(history, RowSet(tuple(TRAIN)))["categories"], 0)
    folded = history.encode(chunk, vocab, rates, True)
    plain = history.encode(chunk, vocab, rates, False)
    assert any(not np.array_equal(folded[n], plain[n], equal_nan=True) for n in NOTICE_RATES)
    assert all(np.array_equal(folded[n], plain[n], equal_nan=True) for n in ARRIVAL_NUMERIC)


def test_a_july_rows_fold_ignores_july_even_when_july_counts_change(history, db):  # noqa: F811
    from dbrel.ml.features import NOTICE_RATES

    chunk = _train_chunk(history)
    july = chunk["day"] < int(np.datetime64("2026-08-01", "D").astype(np.int64))
    before = _rates(db).rates(chunk["station_id"], chunk["line_id"], chunk["train_id"], chunk["day"], fold=True)
    _execute(db, "UPDATE notice_rate_daily SET n = n * 3, k_works = k_works * 3 + 1, "
                 "k_disruption = k_disruption * 3 + 1 WHERE service_date LIKE '2026-07-%'")
    after = _rates(db).rates(chunk["station_id"], chunk["line_id"], chunk["train_id"], chunk["day"], fold=True)
    for name in NOTICE_RATES:
        assert np.array_equal(before[name][july], after[name][july], equal_nan=True), name  # July rows: July is out
    assert any(not np.array_equal(before[n][~july], after[n][~july], equal_nan=True) for n in NOTICE_RATES)


def test_prepared_inputs_follow_the_rates_and_changed_counts_rebuild_them(history, db, tmp_path):  # noqa: F811
    from dbrel.ml.features import NOTICE_NUMERIC

    rows = RowSet(tuple(TRAIN))
    rates = _rates(db)
    stats = training_statistics(history, rows, numeric=NOTICE_NUMERIC, rates=rates, fold=True)
    vocab = training_vocabulary(stats["categories"], stats["last_day"])
    prep = prep_from_statistics(stats, vocab, "standard", NOTICE_NUMERIC)
    root = tmp_path / "prepared"
    first = prepare(history, rows, vocab, prep, root, "fit", rates=rates, fold=True)
    encoded = history.encode(_train_chunk(history), vocab, rates, True)
    _, nums = arrival_inputs(encoded, prep)
    assert np.array_equal(first.nums, nums)
    first_nums = np.array(first.nums)  # the directory is rewritten below; keep what it held
    assert first.key["notice_rates"] == {"fingerprint": rates.fingerprint(), "fold": True}
    inode = (root / "fit" / "nums.bin").stat().st_ino
    assert (prepare(history, rows, vocab, prep, root, "fit", rates=rates, fold=True).dir / "nums.bin"
            ).stat().st_ino == inode  # same counts: reused

    _execute(db, "UPDATE notice_rate_daily SET k_works = k_works + 1 WHERE service_date = '2026-08-01'")
    changed = _rates(db)
    assert changed.fingerprint() != rates.fingerprint()
    with pytest.raises(CheckpointMismatch, match="notice_rates"):
        prepare(history, rows, vocab, prep, root, "fit", rates=changed, fold=True)
    rebuilt = prepare(history, rows, vocab, prep, root, "fit", rates=changed, fold=True, replace=True)
    assert rebuilt.key["notice_rates"]["fingerprint"] == changed.fingerprint()
    assert not np.array_equal(rebuilt.nums, first_nums)


def test_a_prep_that_lists_notice_rates_needs_them(history):  # noqa: F811
    from dbrel.ml.features import NOTICE_NUMERIC

    with pytest.raises(ValueError, match="notice rates"):
        training_statistics(history, RowSet(tuple(TRAIN)), numeric=NOTICE_NUMERIC)
    rows = RowSet(tuple(TRAIN))
    stats = training_statistics(history, rows)
    vocab = training_vocabulary(stats["categories"], stats["last_day"])
    prep = {**prep_from_statistics(stats, vocab, "standard"), "features_numeric": list(NOTICE_NUMERIC)}
    with pytest.raises(ValueError, match="pass the notice rates"):
        prepare(history, rows, vocab, prep, history.root.parent / "p", "x")


def test_a_memory_check_made_before_the_notice_rates_is_refused(history, tmp_path):  # noqa: F811
    work = tmp_path / "work"
    _check(history, work, "large", peak=2 * 2**30)
    path = memory_check_path(work, "large")
    old = {k: v for k, v in json.loads(path.read_text()).items() if k != "notice_rates_bytes"}
    path.write_text(json.dumps(old))
    with pytest.raises(LaunchError, match="predates the notice-rate inputs"):
        gate([SEARCH + ["--work", str(work)]], "14G", tmp_path / "ml")
