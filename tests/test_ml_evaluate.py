"""The baseline the model has to beat, and the metrics that decide it.

The baseline is rebuilt here from the fit period rather than read from
train_stop_average, because that table is computed over every date including the
test period. Testing that distinction is the point of most of this file: a
baseline that has seen the answers is unbeatable, and a model that "loses" to it
would be rejected for the wrong reason.

No lightgbm needed — everything takes plain lists.
"""

from __future__ import annotations

from dbrel.ml._common import coverage, mae, mean_width, pinball_loss
from dbrel.ml.evaluate import (
    baseline_predictions,
    evaluate,
    historical_baseline,
)
from dbrel.ml.features import Dataset


def dataset(keys, labels, dates=None):
    data = Dataset()
    data.keys = list(keys)
    data.labels = [float(v) for v in labels]
    data.dates = list(dates or ["2026-09-01"] * len(keys))
    data.rows = [[] for _ in keys]
    return data


KEY_A = ("WEEKDAY", 1, 10, 30000)
KEY_B = ("WEEKDAY", 2, 10, 31000)


class TestHistoricalBaseline:
    def test_it_is_the_median_delay_per_key(self):
        data = dataset([KEY_A] * 5, [0, 60, 120, 180, 240])
        assert historical_baseline(data, [0, 1, 2, 3, 4])[KEY_A] == 120.0

    def test_it_only_sees_the_rows_it_is_given(self):
        """The leak this guards: a baseline built over the test period too would
        be comparing the model against something that has read the answers."""
        data = dataset([KEY_A] * 4, [0, 0, 0, 10_000])
        fit_only = historical_baseline(data, [0, 1, 2])
        assert fit_only[KEY_A] == 0.0

    def test_keys_never_seen_do_not_appear(self):
        data = dataset([KEY_A, KEY_B], [100, 200])
        assert set(historical_baseline(data, [0])) == {KEY_A}


class TestBaselinePredictions:
    def test_a_known_key_uses_its_history(self):
        data = dataset([KEY_A, KEY_A], [100, 100])
        lookup = {KEY_A: 100.0}
        predictions, cold = baseline_predictions(data, [0, 1], lookup, fallback=7.0)
        assert predictions == [100.0, 100.0]
        assert cold == 0.0

    def test_an_unseen_key_falls_back_and_is_counted_as_cold(self):
        """Cold starts are where a model has a structural edge over a lookup,
        so the share is reported rather than quietly absorbed."""
        data = dataset([KEY_A, KEY_B], [100, 200])
        predictions, cold = baseline_predictions(data, [0, 1], {KEY_A: 100.0}, fallback=7.0)
        assert predictions == [100.0, 7.0]
        assert cold == 0.5


class TestMetrics:
    def test_pinball_at_the_median_is_half_the_absolute_error(self):
        assert pinball_loss([100.0], [0.0], 0.5) == 50.0

    def test_pinball_punishes_a_high_quantile_asymmetrically(self):
        """Why MAE cannot be the yardstick for a p90 model: sitting above most
        observations is the correct behaviour, and MAE would call it error."""
        over = pinball_loss([0.0], [100.0], 0.9)
        under = pinball_loss([100.0], [0.0], 0.9)
        assert under > over

    def test_mae_is_the_mean_absolute_error(self):
        assert mae([10.0, 20.0], [0.0, 0.0]) == 15.0

    def test_coverage_counts_what_lands_inside(self):
        assert coverage([1.0, 5.0, 11.0], [0.0] * 3, [10.0] * 3) == 2 / 3

    def test_width_is_reported_because_coverage_alone_is_gameable(self):
        assert mean_width([0.0, 0.0], [10.0, 20.0]) == 15.0


class TestEvaluation:
    def test_improvement_is_relative_to_the_baseline(self):
        result = evaluate(
            actual=[100.0] * 4,
            model_p50=[90.0] * 4,
            band_lo=[0.0] * 4,
            band_hi=[200.0] * 4,
            baseline=[80.0] * 4,
            naive=50.0,
            cold_start_share=0.0,
        )
        # model is 10 off, baseline 20 off -> half the loss
        assert result.improvement == 0.5

    def test_a_worse_model_reports_a_negative_improvement(self):
        result = evaluate(
            actual=[100.0] * 4, model_p50=[50.0] * 4, band_lo=[0.0] * 4,
            band_hi=[200.0] * 4, baseline=[95.0] * 4, naive=50.0, cold_start_share=0.0,
        )
        assert result.improvement < 0

    def test_a_perfect_baseline_does_not_divide_by_zero(self):
        result = evaluate(
            actual=[100.0] * 4, model_p50=[90.0] * 4, band_lo=[0.0] * 4,
            band_hi=[200.0] * 4, baseline=[100.0] * 4, naive=50.0, cold_start_share=0.0,
        )
        assert result.improvement == 0.0
