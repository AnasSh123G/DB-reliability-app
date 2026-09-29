"""Arrival-only candidates: forecast contract, decision rules, input prep, histogram."""

from __future__ import annotations

import numpy as np
import pytest

from dbrel.ml.arrival import (
    HIST_COLUMNS, NO_HISTORY, ArrivalForecast, ConditionalHistogram, arrival_inputs, bin_edges, bin_index,
    distribution_point, fit_input_prep, grouped_quantiles, grouped_window, histogram_inputs,
)
from dbrel.ml.arrival_runs import rank_trials, trial_rank_key
from dbrel.ml.features import ARRIVAL_NUMERIC, CATEGORICAL, UNKNOWN
from dbrel.ml.metrics import point_concentration, score_arrivals, score_arrivals_with_breakdowns


def _encoded(n, **columns):
    out = {c: np.zeros(n, dtype=np.int32) for c in CATEGORICAL}
    for name in ARRIVAL_NUMERIC:
        out[name] = np.zeros(n, dtype=np.float32)
    out["sched_min"] = np.full(n, 8 * 60, dtype=np.float32)
    for name, values in columns.items():
        out[name] = np.asarray(values, dtype=out[name].dtype)
    return out


# --- forecast contract --------------------------------------------------------

def test_forecast_requires_finite_points_or_a_reason():
    ok = ArrivalForecast.build([1.0, 7.0], ["", "no_training_history"], "window")
    assert np.isnan(ok.point[1]) and ok.percentiles is None
    with pytest.raises(ValueError, match="finite"):
        ArrivalForecast(np.array([np.nan], np.float32), np.array([""]), "window")
    with pytest.raises(ValueError, match="NaN point"):
        ArrivalForecast(np.array([2.0], np.float32), np.array(["x"]), "window")
    with pytest.raises(ValueError, match="ordered"):
        ArrivalForecast.build([1.0], [""], "window", [[2.0, 1.0, 3.0]])
    with pytest.raises(ValueError, match="equal p50"):
        ArrivalForecast.build([1.0], [""], "median", [[0.0, 2.0, 3.0]])
    with pytest.raises(ValueError, match="point rule"):
        ArrivalForecast.build([1.0], [""], "p50")


# --- selection and reporting ------------------------------------------------------

def test_trials_rank_by_mae_with_full_coverage_first():
    def trial(mae, coverage, size):
        return {"summary": {"validation": {"mae_scored": mae, "coverage": coverage}, "asset_bytes": size}}
    trials = {"partial": trial(1.0, 0.9, 1), "big": trial(2.0, 1.0, 9), "small": trial(2.0, 1.0, 5),
              "best": trial(1.5, 1.0, 50), "empty": trial(None, 0.0, 1)}
    assert rank_trials(trials) == ["best", "small", "big", "partial", "empty"]
    assert trial_rank_key({"mae_scored": 1.0, "coverage": 1.0}, 0) < trial_rank_key(
        {"mae_scored": 0.5, "coverage": 0.99}, 0)


def test_report_flags_a_near_constant_estimate_and_breaks_down_by_month():
    actual = np.array([0.0, 2.0, 5.0, 9.0])
    predicted = np.array([3.0, 3.0, 3.04, np.nan])
    assert point_concentration(predicted) == {"most_common_min": 3.0, "most_common_share": 1.0}
    out = score_arrivals_with_breakdowns(actual, predicted,
                                         {"month": np.array(["2025-12", "2025-12", "2026-01", "2026-01"])})
    assert out["overall"]["most_common_share"] == 1.0
    assert set(out["by_month"]) == {"2025-12", "2026-01"}
    assert out["by_month"]["2026-01"]["coverage"] == 0.5


# --- decision rules -------------------------------------------------------------

def test_window_point_beats_the_median_on_a_two_cluster_distribution():
    values = np.array([0, 0, 0.5, 1, 1.5, 10, 10.5, 11, 11.5, 12], dtype=np.float64)
    centre, share = grouped_window(values, np.array([0]), np.array([len(values)]))
    (_, median, _), = grouped_quantiles(values, np.array([0]), np.array([len(values)]))
    assert centre[0] == pytest.approx(0.75) and share[0] == pytest.approx(0.5)  # tie: lowest start
    assert median == pytest.approx(5.75)  # between the clusters, near no actual arrival
    hits = lambda p: score_arrivals(values, np.full(len(values), p))["within_3_min"]  # noqa: E731
    assert hits(centre[0]) > hits(median)


def test_window_is_inclusive_and_groups_are_independent():
    values = np.array([0.0, 6.0, 100.0, 100.0, 107.0])
    centre, share = grouped_window(values, np.array([0, 2]), np.array([2, 3]))
    assert centre.tolist() == [3.0, 100.0]  # 0 and 6 both lie exactly 3 from 3
    assert share.tolist() == [1.0, pytest.approx(2 / 3)]


def test_binned_distribution_point_and_percentiles():
    edges = bin_edges(-5, 20)
    probs = np.zeros((2, len(edges) - 1))
    probs[0, bin_index(np.array([0.5]), edges)] = 0.4    # a narrow early peak ...
    probs[0, bin_index(np.array([10.5, 11.5, 12.5, 13.5]), edges)] = 0.15  # ... vs a wider late one
    probs[1, :] = 1.0                                     # flat: ordered percentiles
    window, pct = distribution_point(probs, edges, "window")
    median, pct_m = distribution_point(probs, edges, "median")
    assert window[0] == pytest.approx(11.0)       # window [8, 14] holds 0.6 > 0.4
    assert np.all(np.diff(pct, axis=1) >= 0) and np.allclose(pct, pct_m)
    assert median.tolist() == pct[:, 1].tolist()
    assert pct[1].tolist() == pytest.approx([1.25, 7.5, 13.75])
    assert bin_index(np.array([-99.0, 999.0]), edges).tolist() == [0, len(edges) - 2]


# --- network inputs -------------------------------------------------------------

def test_input_prep_unknowns_and_training_only_statistics():
    vocab = {c: [0, 1] for c in CATEGORICAL}
    enc = _encoded(4, train=[0, 1, UNKNOWN, 7], lat=[1.0, 3.0, np.nan, 1000.0])
    train = np.array([True, True, False, False])
    prep = fit_input_prep(enc, train, vocab, scaling="robust")
    cats, nums = arrival_inputs(enc, prep)
    t = CATEGORICAL.index("train")
    assert prep["cards"][t] == 3
    assert cats[:, t].tolist() == [1, 2, 0, 0]       # UNKNOWN and an out-of-vocab code -> slot 0
    assert prep["stats"]["lat"][0] == 2.0            # median of training rows only
    assert "lat_missing" not in prep["numeric"]      # training rows had no missing lat
    lat = nums[:, prep["numeric"].index("lat")]
    assert lat[2] == 0.0 and abs(lat[3]) < 3.0       # NaN -> 0; smooth clipping bounds outliers
    enc2 = dict(enc, lat=np.array([1.0, 3.0, 50.0, 60.0], np.float32))
    assert fit_input_prep(enc2, train, vocab, "robust")["stats"] == prep["stats"]
    with pytest.raises(ValueError, match="scaling"):
        fit_input_prep(enc, train, vocab, scaling="minmax")


def test_time_of_year_inputs_pass_through_unscaled_and_are_recorded():
    vocab = {c: [0, 1] for c in CATEGORICAL}
    angle = 2 * np.pi * np.array([0, 91, 182, 300]) / 366
    enc = _encoded(4, doy_sin=np.sin(angle), doy_cos=np.cos(angle))
    train = np.array([True, True, False, False])
    prep = fit_input_prep(enc, train, vocab, scaling="robust", numeric=ARRIVAL_NUMERIC)
    assert prep["features_numeric"] == list(ARRIVAL_NUMERIC)
    assert prep["stats"]["doy_sin"] == [0.0, 1.0] == prep["stats"]["doy_cos"]
    _, nums = arrival_inputs(enc, prep)
    got = nums[:, [prep["numeric"].index("doy_sin"), prep["numeric"].index("doy_cos")]]
    assert np.allclose(got, np.column_stack([np.sin(angle), np.cos(angle)]), atol=1e-6)


def test_prep_without_a_feature_list_still_means_the_original_inputs():
    vocab = {c: [0, 1] for c in CATEGORICAL}
    enc = _encoded(3)
    prep = fit_input_prep(enc, np.array([True, True, False]), vocab)
    assert "features_numeric" not in prep  # deployed artifacts keep their exact prep
    _, nums = arrival_inputs(enc, prep)
    assert nums.shape[1] == len(prep["numeric"]) and "doy_sin" not in prep["numeric"]


# --- conditional histogram -------------------------------------------------------

def _history():
    """Train 0 at station 0: 12 normal-day arrivals at 1..3 min plus 3 hard cases at 20+
    and 12 holiday arrivals at 8 min; train 1 (same line) has too little history."""
    y = [1, 1, 2, 2, 2, 2, 3, 3, 2, 1, 2, 2, 20, 25, 30] + [8] * 12 + [5, 6]
    n = len(y)
    holiday = [0] * 15 + [1] * 12 + [0, 0]
    train = [0] * 27 + [1, 1]
    return _encoded(n, train=train, is_holiday=holiday), np.array(y, np.float32)


def test_histogram_uses_exact_key_then_backs_off_by_support():
    enc, y = _history()
    model = ConditionalHistogram(min_support=10, point_rule="window").fit(enc, y, np.ones(len(y), bool))
    query = _encoded(4, train=[0, 0, 1, UNKNOWN], is_holiday=[0, 1, 0, 0])
    out = model.predict(query)
    assert out.point.shape == (4,) and out.percentiles.shape == (4, 3)
    assert out.detail["level"].tolist() == ["train_station", "train_station",
                                           "line_station_hour", "line_station_hour"]
    assert out.point[0] == pytest.approx(2.0)          # densest window of train 0, normal days
    assert out.point[1] == pytest.approx(8.0)          # day type kept: holiday history only
    assert out.detail["support"].tolist() == [15, 12, 17, 17]
    assert np.all(np.diff(out.percentiles, axis=1) >= 0)
    assert (out.missing_reason == "").all()


def test_histogram_median_rule_and_day_type_fallback():
    enc, y = _history()
    train_only_normal = np.array(enc["is_holiday"]) == 0
    model = ConditionalHistogram(min_support=17, point_rule="median").fit(enc, y, train_only_normal)
    out = model.predict(_encoded(2, is_holiday=[0, 1]))
    # normal day: train 0 has 15 < 17, its line at that hour has 17; no holiday history -> global
    assert out.detail["level"].tolist() == ["line_station_hour", "global"]
    assert out.point.tolist() == out.percentiles[:, 1].tolist()
    assert out.rule == "median"


def test_histogram_never_reads_evaluated_outcomes():
    enc, y = _history()
    train = np.arange(len(y)) < 27
    first = ConditionalHistogram(min_support=1).fit(enc, y, train).predict(enc)
    y2 = y.copy()
    y2[~train] = 99.0
    second = ConditionalHistogram(min_support=1).fit(enc, y2, train).predict(enc)
    assert np.array_equal(first.point, second.point)


def test_histogram_without_any_matching_level_is_explicitly_missing():
    enc, y = _history()
    levels = ConditionalHistogram().levels[:1]  # exact key only, no fallback
    model = ConditionalHistogram(min_support=1, levels=levels).fit(enc, y, np.ones(len(y), bool))
    out = model.predict(_encoded(1, train=[UNKNOWN]))
    assert out.missing_reason.tolist() == [NO_HISTORY] and np.isnan(out.point[0])
    assert model.asset_bytes() == 28 * 3  # three (train, station, day type) keys
    with pytest.raises(ValueError, match="training arrivals"):
        ConditionalHistogram().fit(enc, y, np.zeros(len(y), bool))


def test_histogram_export_round_trips_and_drops_unusable_keys(tmp_path):
    enc, y = _history()
    model = ConditionalHistogram(min_support=10).fit(enc, y, np.ones(len(y), bool))
    files = model.export(tmp_path)
    assert files == ["histogram.json", "histogram.bin"]
    loaded = ConditionalHistogram.load_export(tmp_path)
    query = _encoded(4, train=[0, 0, 1, UNKNOWN], is_holiday=[0, 1, 0, 0])
    a, b = model.predict(query), loaded.predict(query)
    assert np.array_equal(a.point, b.point) and np.array_equal(a.percentiles, b.percentiles)
    assert a.detail["level"].tolist() == b.detail["level"].tolist()
    padding = (tmp_path / "histogram.bin").stat().st_size - model.asset_bytes()  # 28 bytes per key
    assert 0 <= padding < 8 * len(model.levels)
    assert histogram_inputs(query).shape == (4, len(HIST_COLUMNS))
