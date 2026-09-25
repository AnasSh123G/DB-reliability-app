"""Split, metrics, baselines, candidates, tuner and the bake-off artifact.

The smoke frames here are tiny random arrays built in memory for these tests
only; nothing is written to any database.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from dbrel.ml import baselines
from dbrel.ml.candidates import CatBoostCandidate, LightGBMCandidate, XGBoostCandidate
from dbrel.ml.features import CATEGORICAL, FEATURES, NO_LINE, UNKNOWN
from dbrel.ml.metrics import order_quantiles, pinball, score
from dbrel.ml.split import masks_for, restrict_to_training_vocab, split_dates
from dbrel.ml.tune import tune

# --- split ------------------------------------------------------------------


def test_split_is_chronological_disjoint_and_drops_stubs():
    rows = {f"2026-09-{d:02d}": 500 for d in range(1, 31)}
    rows["2026-08-31"] = 20  # stub
    split = split_dates(rows)
    assert "2026-08-31" not in split.train + split.validation + split.test
    assert (len(split.train), len(split.validation), len(split.test)) == (20, 4, 6)
    assert split.train[-1] < split.validation[0] and split.validation[-1] < split.test[0]
    assert not set(split.train) & set(split.validation) | set(split.validation) & set(split.test)


def test_no_run_spans_two_sets():
    dates = [f"2026-09-{d:02d}" for d in range(1, 11)]
    split = split_dates({d: 100 for d in dates})
    rng = np.random.default_rng(0)
    trip_day = rng.integers(0, 10, size=200)  # each trip has one service date
    trip_id = np.repeat(np.arange(200), 5)
    day = np.repeat(trip_day, 5)
    masks = masks_for(day, dates, split)
    for mask in masks.values():
        inside = set(trip_id[mask])
        others = set(trip_id[~mask])
        assert not inside & others


def test_training_vocab_makes_test_only_values_unknown():
    features = {name: np.zeros(4, dtype=np.int32) for name in CATEGORICAL}
    features["train"] = np.array([0, 1, 2, 2], dtype=np.int32)
    vocab = {name: [0] for name in CATEGORICAL}
    vocab["train"] = [100, 200, 300]
    train_mask = np.array([True, False, True, False])
    out, new_vocab = restrict_to_training_vocab(features, vocab, train_mask)
    assert new_vocab["train"] == [100, 300]
    assert out["train"].tolist() == [0, UNKNOWN, 1, 1]


# --- metrics ----------------------------------------------------------------


def test_pinball_and_coverage_by_hand():
    y = np.array([0.0, 2.0, 4.0, 10.0])
    assert pinball(y, np.full(4, 2.0), 0.5) == pytest.approx((1 + 0 + 1 + 4) / 4)
    assert pinball(y, np.full(4, 2.0), 0.25) == pytest.approx((0.75 * 2 + 0 + 0.25 * 2 + 0.25 * 8) / 4)
    pred = np.tile([1.0, 2.0, 5.0], (4, 1))
    s = score(y, pred)
    assert s["band_coverage"] == pytest.approx(0.5)  # 2 and 4 inside [1, 5]
    assert s["mae_p50"] == pytest.approx((2 + 0 + 2 + 8) / 4)


def test_uncovered_rows_are_excluded_and_counted():
    y = np.array([1.0, 2.0, 3.0])
    pred = np.array([[0, 1, 2], [np.nan] * 3, [2, 3, 4]], dtype=float)
    s = score(y, pred, covered=np.array([True, False, True]))
    assert s["covered_share"] == pytest.approx(2 / 3)
    assert s["mae_p50"] == pytest.approx(0.0)


def test_crossed_quantiles_are_sorted():
    ordered, crossed = order_quantiles(np.array([[3.0, 1.0, 2.0], [0.0, 1.0, 2.0]]))
    assert ordered.tolist() == [[1.0, 2.0, 3.0], [0.0, 1.0, 2.0]]
    assert crossed == pytest.approx(0.5)


# --- baselines ----------------------------------------------------------------


def _baseline_features(n):
    f = {name: np.zeros(n, dtype=np.int32) for name in CATEGORICAL}
    f.update(trip_start_min=np.zeros(n, dtype=np.float32), sched_min=np.zeros(n, dtype=np.float32),
             is_holiday=np.zeros(n, dtype=np.float32))
    return f


def test_averages_baseline_uses_training_rows_and_flags_unseen_keys():
    f = _baseline_features(6)
    f["station"] = np.array([1, 1, 1, 2, 1, 3], dtype=np.int32)
    y = np.array([0.0, 2.0, 4.0, 5.0, 99.0, 7.0])
    train_rows, eval_rows = np.array([0, 1, 2, 3]), np.array([4, 5])
    pred, covered = baselines.averages_baseline(f, y, train_rows, eval_rows, no_line=-2)
    assert covered.tolist() == [True, False]  # station 3 only ever seen on an eval row
    assert pred[0].tolist() == pytest.approx([1.0, 2.0, 3.0])  # quantiles of 0, 2, 4
    assert np.isnan(pred[1]).all()  # nothing invented


def test_averages_baseline_keys_lineless_trains_by_train():
    f = _baseline_features(4)
    f["line"] = np.array([0, 0, 0, 0], dtype=np.int32)  # code 0 == NO_LINE here
    f["train"] = np.array([1, 2, 1, 2], dtype=np.int32)
    y = np.array([1.0, 9.0, 0.0, 0.0])
    pred, covered = baselines.averages_baseline(f, y, np.array([0, 1]), np.array([2, 3]), no_line=0)
    assert covered.all()
    assert pred[:, 1].tolist() == [1.0, 9.0]
    assert baselines.no_line_code({"line": [NO_LINE, 7]}) == 0


# --- candidates -----------------------------------------------------------------


def smoke_frame(n=3000, seed=0):
    rng = np.random.default_rng(seed)
    X = np.zeros((n, len(FEATURES)), dtype=np.float32)
    for i in range(len(CATEGORICAL)):
        X[:, i] = rng.integers(0, 8, n)
    X[rng.random(n) < 0.05, 0] = UNKNOWN
    X[:, len(CATEGORICAL):] = rng.random((n, len(FEATURES) - len(CATEGORICAL)))
    y = (X[:, 0] % 3) + 5 * X[:, len(CATEGORICAL)] + rng.exponential(1.0, n)
    return X, y.astype(np.float32)


@pytest.mark.parametrize("make", [
    lambda: LightGBMCandidate({"num_leaves": 15}, threads=2, max_rounds=60),
    lambda: CatBoostCandidate({"depth": 4}, threads=2, max_rounds=60),
    lambda: XGBoostCandidate({"max_depth": 4}, threads=2, max_rounds=60, device="cpu"),
], ids=["lightgbm", "catboost", "xgboost"])
def test_candidate_predicts_ordered_quantiles_and_round_trips(make, tmp_path):
    X, y = smoke_frame()
    candidate = make()
    candidate.fit(X[:2000], y[:2000], X[2000:], y[2000:])
    pred, crossed = candidate.predict(X[2000:])
    assert pred.shape == (1000, 3)
    assert (np.diff(pred, axis=1) >= 0).all()
    assert 0.0 <= crossed <= 1.0
    candidate.save(tmp_path / "dep")
    loaded = type(candidate).load(tmp_path / "dep", candidate.describe())
    assert np.allclose(loaded.predict(X[2000:])[0], pred, atol=1e-4)


def test_tuner_respects_budget_and_reports_trials():
    X, y = smoke_frame()
    result = tune("lightgbm", X[:2000], y[:2000], X[2000:], y[2000:],
                  budget_s=1.0, max_trials=50, threads=2, max_rounds=40)
    assert result.n_trials >= 1
    assert result.seconds < 30
    assert result.under_tuned == (result.n_trials < 5)
    assert set(result.best_params) >= {"num_leaves", "learning_rate"}
