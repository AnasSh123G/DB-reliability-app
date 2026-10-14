"""Small CPU-only checks for frozen summary verification and display rounding."""

from __future__ import annotations

import numpy as np
import pytest

from dbrel.ml.metrics import score_arrivals
from summarize_compact_training import shown, verify_groups


def _frozen_groups(y, points, groups):
    return {
        str(group): score_arrivals(y[groups == group], points[groups == group])
        for group in np.unique(groups)
    }


def test_verify_groups_accepts_the_points_that_generated_each_frozen_group_score():
    y = np.array([0.0, 0.0, 0.0])
    groups = np.array(["A", "B", "C"])
    points = np.array([0.0, 0.0, 6.0])
    frozen = _frozen_groups(y, points, groups)

    checked = verify_groups(y, points, groups, frozen, str)

    assert checked == frozen


def test_verify_groups_rejects_reordered_points_even_when_overall_scores_match():
    y = np.array([0.0, 0.0, 0.0])
    groups = np.array(["A", "B", "C"])
    points = np.array([0.0, 0.0, 6.0])
    reordered = np.array([6.0, 0.0, 0.0])
    frozen = _frozen_groups(y, points, groups)

    assert score_arrivals(y, points) == score_arrivals(y, reordered)
    with pytest.raises(ValueError, match="group A: saved points differ from frozen metric"):
        verify_groups(y, reordered, groups, frozen, str)


def test_verify_groups_rejects_missing_frozen_group_keys():
    y = np.array([0.0, 0.0, 0.0])
    groups = np.array(["A", "B", "C"])
    points = np.array([0.0, 0.0, 6.0])
    frozen = _frozen_groups(y, points, groups)
    del frozen["B"]

    with pytest.raises(ValueError, match="saved prediction groups differ from frozen groups"):
        verify_groups(y, points, groups, frozen, str)


def test_shown_matches_subminute_clamp_and_positive_math_round_boundaries():
    points = np.array([-3.0, 0.0, 0.999, 1.0, 1.499, 1.5, 2.5])

    assert np.array_equal(shown(points), np.array([0.0, 0.0, 0.0, 1.0, 1.0, 2.0, 3.0]))
