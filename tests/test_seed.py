"""The delay profiles are magic numbers until something checks them.

punctual_rate and target_mean_s are the two published figures the simulation is
meant to reproduce, and they constrain each other through the shape parameter --
so they are asserted here rather than asserted in a comment.
"""

from __future__ import annotations

import random
import statistics

import pytest

from dbrel.config import PUNCTUAL_S
from dbrel.seed import SEGMENT_PROFILES, sample_delay

DRAWS = 200_000


@pytest.fixture(scope="module")
def draws() -> dict[str, list[int]]:
    return {
        segment: [sample_delay(random.Random(4242 + i), profile) for i in range(DRAWS)]
        for segment, profile in SEGMENT_PROFILES.items()
    }


@pytest.mark.parametrize("segment", sorted(SEGMENT_PROFILES))
def test_realised_punctuality_matches_target(segment, draws):
    """Share under six minutes must land within 2pp of the published figure."""
    profile = SEGMENT_PROFILES[segment]
    realised = sum(1 for d in draws[segment] if d < PUNCTUAL_S) / DRAWS
    assert realised == pytest.approx(profile.punctual_rate, abs=0.02), (
        f"{segment}: punctuality {realised:.3f}, wanted {profile.punctual_rate:.3f}"
    )


@pytest.mark.parametrize("segment", sorted(SEGMENT_PROFILES))
def test_realised_mean_matches_target(segment, draws):
    """Mean delay must land within half a minute of the published figure."""
    profile = SEGMENT_PROFILES[segment]
    realised = statistics.fmean(draws[segment])
    assert realised == pytest.approx(profile.target_mean_s, abs=30.0), (
        f"{segment}: mean {realised / 60:.2f} min, wanted {profile.target_mean_s / 60:.2f} min"
    )


@pytest.mark.parametrize("segment", sorted(SEGMENT_PROFILES))
def test_distribution_is_right_skewed(segment, draws):
    """The median must sit well below the mean: delay is not symmetric."""
    sample = draws[segment]
    assert statistics.median(sample) < statistics.fmean(sample)


def test_long_distance_is_the_worst_segment(draws):
    """FV must be materially later than RV, and RV later than S."""
    means = {segment: statistics.fmean(sample) for segment, sample in draws.items()}
    assert means["FV"] > means["RV"] > means["S"]
