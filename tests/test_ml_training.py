"""Split, metrics, baselines, candidates, tuner and the bake-off artifact.

The smoke frames here are tiny random arrays built in memory for these tests
only; nothing is written to any database.
"""

from __future__ import annotations


import numpy as np
import pytest

from dbrel.ml.candidates import CatBoostCandidate, LightGBMCandidate, XGBoostCandidate
from dbrel.ml.features import CATEGORICAL, FEATURES, UNKNOWN
from dbrel.ml.metrics import (
    pinball, score,
)
from dbrel.ml.split import (
    split_dates,
    training_history_before,
)

# --- split ------------------------------------------------------------------


def test_split_is_chronological_disjoint_and_drops_stubs():
    rows = {f"2026-09-{d:02d}": 500 for d in range(1, 31)}
    rows["2026-08-31"] = 20  # stub
    split = split_dates(rows)
    assert "2026-08-31" not in split.train + split.validation + split.test
    assert (len(split.train), len(split.validation), len(split.test)) == (20, 4, 6)
    assert split.train[-1] < split.validation[0] and split.validation[-1] < split.test[0]
    assert not set(split.train) & set(split.validation) | set(split.validation) & set(split.test)


def test_history_excludes_labels_finalized_after_cross_midnight_cutoff():
    from ml_fixture import utc_of

    # Service-date 2 September, arrival 03:00 on the 3rd: the cutoff is
    # 22:00 on the 2nd, even though the scheduled arrival falls the next day.
    arrival = utc_of("2026-09-02", 27 * 3600)
    cutoff = arrival - 5 * 3600
    mask = training_history_before(
        np.array([True, True, True, False]),
        np.array([cutoff]),
        np.array([cutoff - 1, cutoff + 1, -1, cutoff - 100]),
    )
    assert mask.tolist() == [True, False, False, False]
    with pytest.raises(ValueError, match="no finalized"):
        training_history_before(np.array([True]), np.array([cutoff]), np.array([cutoff + 1]))


# --- metrics ----------------------------------------------------------------


def test_pinball_and_coverage_by_hand():
    y = np.array([0.0, 2.0, 4.0, 10.0])
    assert pinball(y, np.full(4, 2.0), 0.5) == pytest.approx((1 + 0 + 1 + 4) / 4)
    assert pinball(y, np.full(4, 2.0), 0.25) == pytest.approx((0.75 * 2 + 0 + 0.25 * 2 + 0.25 * 8) / 4)
    pred = np.tile([1.0, 2.0, 5.0], (4, 1))
    s = score(y, pred)
    assert s["band_coverage"] == pytest.approx(0.5)  # 2 and 4 inside [1, 5]
    assert s["mae_p50"] == pytest.approx((2 + 0 + 2 + 8) / 4)


# --- baselines ----------------------------------------------------------------


def _baseline_features(n):
    f = {name: np.zeros(n, dtype=np.int32) for name in CATEGORICAL}
    f.update(trip_start_min=np.zeros(n, dtype=np.float32), sched_min=np.zeros(n, dtype=np.float32),
             is_holiday=np.zeros(n, dtype=np.float32))
    return f


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


