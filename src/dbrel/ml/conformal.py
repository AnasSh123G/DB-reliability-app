"""Turning a quantile band into one with a coverage guarantee.

A lo/hi pair from a quantile model is a band, but nothing makes it hit its
target coverage: the model was fitted to minimise pinball loss, not to be right
a stated share of the time, and on unseen data it is usually somewhat too
narrow. Split conformal fixes that after the fact. Fit on one slice, measure how
far outside the band the truth actually fell on a second, disjoint slice, and
widen by the right order statistic of those misses.

The guarantee that buys is distribution-free and finite-sample: it holds without
assuming the delays are normal, or that the model is any good, only that the
calibration rows and the rows being predicted are exchangeable. That last word
is why calibration comes from held-out *dates* rather than random rows — see the
split note in features.py.

This is conformalized quantile regression (CQR): the widening is a single number
but the band it widens is already adaptive, so a stop the model finds erratic
keeps a wider interval than one it finds predictable.

Pure Python over plain lists. No lightgbm, no numpy — this is testable on an
install that never took the `ml` extra, and it is the part most worth testing,
because a silently wrong adjustment produces a confident lie.
"""

from __future__ import annotations

import math

# Returned when there are too few calibration points to honour the requested
# coverage. An infinite band is useless but honest; a finite one would be a
# guarantee we cannot make.
NO_GUARANTEE = float("inf")


def conformity_scores(
    actual: list[float], lo: list[float], hi: list[float]
) -> list[float]:
    """How far outside its band each observation fell.

    Negative when the truth sat comfortably inside, which matters: those rows are
    evidence the band is too *wide*, and including them is what lets calibration
    tighten a band as well as widen one.
    """
    return [max(low - a, a - high) for a, low, high in zip(actual, lo, hi)]


def adjustment(scores: list[float], alpha: float = 0.1) -> float:
    """The amount to widen a band by, for 1 - alpha coverage.

    The rank is ceil((n + 1) * (1 - alpha)), not a percentile of the scores. The
    finite-sample proof is a statement about that exact order statistic — the
    (n + 1) is what pays for the row being predicted, which is not in the
    calibration set — so this deliberately does not reuse derive/_common.percentile,
    whose nearest-rank rounding would be off by one and would quietly turn a
    guarantee into an approximation.
    """
    if not scores:
        return NO_GUARANTEE
    ordered = sorted(scores)
    n = len(ordered)
    rank = math.ceil((n + 1) * (1 - alpha))
    if rank > n:
        # Fewer than 1/alpha - 1 calibration points: no finite band can promise
        # this coverage, so say so rather than inventing one.
        return NO_GUARANTEE
    return ordered[rank - 1]


def calibrate(
    actual: list[float], lo: list[float], hi: list[float], alpha: float = 0.1
) -> float:
    """Convenience: scores then adjustment, the whole calibration step."""
    return adjustment(conformity_scores(actual, lo, hi), alpha)


def apply(lo: list[float], hi: list[float], widen: float) -> tuple[list[float], list[float]]:
    """Widen (or tighten, when the adjustment is negative) a predicted band."""
    if widen == NO_GUARANTEE:
        return ([-NO_GUARANTEE] * len(lo), [NO_GUARANTEE] * len(hi))
    return ([low - widen for low in lo], [high + widen for high in hi])
