"""Split, metrics, baselines, candidates, tuner and the bake-off artifact.

The smoke frames here are tiny random arrays built in memory for these tests
only; nothing is written to any database.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from dbrel.ml import baselines
from dbrel.ml.candidates import CatBoostCandidate, LightGBMCandidate, XGBoostCandidate
from dbrel.ml.features import CATEGORICAL, FEATURES, NO_LINE, UNKNOWN
from dbrel.ml.metrics import (
    arrival_errors, order_quantiles, pinball, point_concentration, score, score_arrivals,
    score_arrivals_with_breakdowns,
)
from dbrel.ml.report import save_arrival_report
from dbrel.ml.split import (
    arrival_experiment_masks, masks_for, reserve_fresh_holdout, restrict_to_training_vocab, split_dates,
    training_history_before,
)
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


def test_fresh_holdout_excludes_inspected_dates_and_stubs():
    rows = {f"2026-09-{day:02d}": 100 for day in range(1, 27)}
    rows["2026-09-23"] = 2
    fresh = reserve_fresh_holdout(
        rows, training=["2026-09-01"], validation=["2026-09-16"],
        inspected=[f"2026-09-{day:02d}" for day in range(17, 23)],
    )
    assert fresh == ["2026-09-24", "2026-09-25", "2026-09-26"]
    with pytest.raises(ValueError, match="only 2 available"):
        reserve_fresh_holdout(
            dict(rows, **{"2026-09-26": 1}), training=["2026-09-01"],
            validation=["2026-09-16"], inspected=["2026-09-22"],
        )


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


def test_arrival_experiment_fits_vocab_only_from_finalized_prior_history():
    dates = ["2026-09-01", "2026-09-02", "2026-09-03"]
    ds = SimpleNamespace(
        day=np.array([0, 0, 1, 2]), cutoff_utc=np.array([100, 100, 200, 300]),
        finalized_at=np.array([150, 250, 300, 400]),
        meta={"dates": dates, "rows_per_date": dict.fromkeys(dates, 100),
              "timetable_asof_verified": False},
    )
    split, masks = arrival_experiment_masks(
        ds, training=[dates[0]], validation=[dates[1]], inspected=[dates[2]],
    )
    assert split.test == []
    assert masks["train"].tolist() == [True, False, False, False]
    feats = {name: np.zeros(4, dtype=np.int32) for name in CATEGORICAL}
    feats["train"] = np.array([0, 1, 2, 3], dtype=np.int32)
    vocab = {name: [0] for name in CATEGORICAL}
    vocab["train"] = [10, 20, 30, 40]
    encoded, trained_vocab = restrict_to_training_vocab(feats, vocab, masks["train"])
    assert trained_vocab["train"] == [10]
    assert encoded["train"].tolist() == [0, UNKNOWN, UNKNOWN, UNKNOWN]
    with pytest.raises(ValueError, match="need 3 complete"):
        arrival_experiment_masks(
            ds, training=[dates[0]], validation=[dates[1]], inspected=[dates[2]],
            final_test=True,
        )


# --- metrics ----------------------------------------------------------------

def test_individual_arrival_tolerances_include_boundary_and_missing():
    actual = np.array([6.0, 10.0, 3.0, 12.0])
    predicted = np.array([4.0, 7.0, np.nan, 0.0])
    scored = score_arrivals(actual, predicted)
    assert scored["eligible"] == 4 and scored["scored"] == 3 and scored["missing"] == 1
    assert scored["coverage"] == 0.75
    assert scored["within_1_min"] == 0
    assert scored["within_3_min"] == 0.5
    assert scored["within_5_min"] == 0.5
    assert scored["mae_scored"] == pytest.approx(17 / 3)
    assert arrival_errors(actual, predicted).tolist()[:2] == [2, 3]
    assert score_arrivals(actual[:1], np.array([2.9999]))["within_3_min"] == 0


def test_arrival_scores_handle_all_missing_empty_and_bad_inputs():
    assert score_arrivals(np.array([2.0]), np.array([np.nan])) == {
        "eligible": 1, "scored": 0, "missing": 1, "coverage": 0.,
        "mae_scored": None, "within_1_min": 0., "within_3_min": 0., "within_5_min": 0.,
    }
    assert score_arrivals(np.array([]), np.array([]))["within_3_min"] is None
    with pytest.raises(ValueError, match="finite"):
        score_arrivals(np.array([np.nan]), np.array([1.]))
    with pytest.raises(ValueError, match="infinite"):
        score_arrivals(np.array([1.]), np.array([np.inf]))


def test_arrival_breakdowns_use_same_individual_rows():
    scores = score_arrivals_with_breakdowns(
        np.array([6., 7., 3.]), np.array([4., np.nan, 3.]),
        {"day_type": np.array([0, 0, 1])},
    )
    assert scores["overall"]["within_3_min"] == pytest.approx(2 / 3)
    assert scores["by_day_type"]["0"]["eligible"] == 2
    assert scores["by_day_type"]["0"]["within_3_min"] == pytest.approx(0.5)


def test_arrival_report_recomputes_scores_from_saved_rows(tmp_path):
    ds = SimpleNamespace(
        stop_id=np.array([11, 12, 13, 14]), trip_id=np.array([1, 2, 3, 4]),
        stop_sequence=np.array([20, 20, 30, 30]), day=np.array([0, 0, 1, 1]),
        segment=np.array(["FV", "FV", "RV", "RV"]),
        arr_min=np.array([1., 4., 12., 80.], dtype=np.float32),
        scheduled_arrival_utc=np.array([100000, 200000, 300000, 400000]),
        cutoff_utc=np.array([82000, 182000, 282000, 382000]),
        features={"station": np.array([1, 1, 2, 2]),
                  "is_holiday": np.array([0., 0., 1., 1.])},
        meta={"dates": ["2026-09-01", "2026-09-02"],
              "timetable_asof_verified": False, "cutoff_hours": 5},
    )
    out = tmp_path / "arrival-report"
    report = save_arrival_report(
        out, ds=ds, rows=np.arange(4),
        predictions={"incumbent": np.array([1., 0., 12., 10.]),
                     "formula": np.array([2., 5., np.nan, 79.])},
        phase="diagnostic", exclusions={"cancelled": 1},
    )
    assert report["exclusions"] == {"cancelled": 1}
    assert report["timetable_asof_verified"] is False
    assert report["scores"]["formula"]["overall"]["within_3_min"] == 0.75
    assert report["scores"]["formula"]["by_date"]["2026-09-02"]["missing"] == 1
    for name, summary in json.loads((out / "report.json").read_text())["scores"].items():
        with np.load(out / f"{name}.npz") as saved:
            recomputed = {**score_arrivals(saved["actual_min"], saved["predicted_min"]),
                          **point_concentration(saved["predicted_min"])}
            assert recomputed == summary["overall"]
            assert np.allclose(saved["error_min"], arrival_errors(
                saved["actual_min"], saved["predicted_min"]), equal_nan=True)
    assert report["scores"]["incumbent"]["overall"]["eligible"] == 4


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
