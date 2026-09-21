"""Split-conformal calibration.

The point of this module is a promise — "the truth lands in this band 90% of the
time" — so the tests check that the promise is kept on data the calibration
never saw, rather than checking the arithmetic against itself.
"""

from __future__ import annotations

import random

import pytest

from dbrel.ml._common import coverage
from dbrel.ml.conformal import NO_GUARANTEE, adjustment, apply, calibrate, conformity_scores


def band(values, half_width):
    return ([v - half_width for v in values], [v + half_width for v in values])


class TestScores:
    def test_a_miss_above_scores_its_distance(self):
        assert conformity_scores([100.0], [0.0], [50.0]) == [50.0]

    def test_a_miss_below_scores_its_distance(self):
        assert conformity_scores([-30.0], [0.0], [50.0]) == [30.0]

    def test_a_hit_scores_negative(self):
        """Comfortably-inside rows are evidence the band is too wide.

        Without them calibration could only ever widen, never tighten.
        """
        assert conformity_scores([25.0], [0.0], [50.0]) == [-25.0]


class TestAdjustment:
    def test_the_rank_is_the_conformal_one_not_a_percentile(self):
        # n=9, alpha=0.1 -> ceil(10*0.9) = 9 -> the largest score.
        scores = [float(i) for i in range(9)]
        assert adjustment(scores, alpha=0.1) == 8.0

    def test_too_few_points_to_promise_the_coverage_says_so(self):
        """With n=5 and alpha=0.1 the required rank is 6, which does not exist."""
        assert adjustment([1.0] * 5, alpha=0.1) == NO_GUARANTEE

    def test_no_points_at_all_says_so(self):
        assert adjustment([], alpha=0.1) == NO_GUARANTEE

    def test_an_over_wide_band_is_tightened(self):
        """A band that never misses gets a negative adjustment, not zero."""
        assert calibrate([0.0] * 50, [-100.0] * 50, [100.0] * 50, alpha=0.1) < 0


class TestCoverage:
    @pytest.mark.parametrize("alpha", [0.1, 0.2, 0.5])
    def test_realised_coverage_matches_the_nominal_level(self, alpha):
        """The whole promise, on rows the calibration never saw.

        Calibrate on one exchangeable sample, measure on another. The band starts
        far too narrow on purpose, so passing means the widening did the work.
        """
        rnd = random.Random(7)
        cal = [rnd.gauss(0, 100) for _ in range(4000)]
        test = [rnd.gauss(0, 100) for _ in range(4000)]

        cal_lo, cal_hi = band([0.0] * len(cal), 5.0)
        widen = calibrate(cal, cal_lo, cal_hi, alpha=alpha)

        test_lo, test_hi = apply(*band([0.0] * len(test), 5.0), widen)
        realised = coverage(test, test_lo, test_hi)
        assert abs(realised - (1 - alpha)) < 0.02

    def test_an_uncalibrated_band_undershoots_its_nominal_coverage(self):
        """The failure conformal exists to fix, pinned so the test above means something."""
        rnd = random.Random(11)
        test = [rnd.gauss(0, 100) for _ in range(4000)]
        lo, hi = band([0.0] * len(test), 5.0)
        assert coverage(test, lo, hi) < 0.10

    def test_a_heteroscedastic_band_keeps_its_shape(self):
        """Calibration shifts a band by one number; it must not flatten it.

        That is the CQR half of the design — the adaptive width comes from the
        quantile models, and conformal only corrects the level.
        """
        rnd = random.Random(3)
        widths = [10.0 + 200.0 * (i % 5) for i in range(2000)]
        actual = [rnd.gauss(0, w / 2) for w in widths]
        lo = [-w for w in widths]
        hi = list(widths)
        widen = calibrate(actual, lo, hi, alpha=0.1)
        new_lo, new_hi = apply(lo, hi, widen)
        spread = [h - low for low, h in zip(new_lo, new_hi)]
        assert max(spread) - min(spread) == pytest.approx(max(widths) * 2 - min(widths) * 2)


def test_no_guarantee_produces_an_infinite_band_rather_than_a_false_one():
    lo, hi = apply([0.0, 0.0], [1.0, 1.0], NO_GUARANTEE)
    assert lo == [-NO_GUARANTEE, -NO_GUARANTEE]
    assert hi == [NO_GUARANTEE, NO_GUARANTEE]
