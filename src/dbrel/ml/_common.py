"""Pieces shared by every part of the delay model.

Same rule as derive/_common.py: there is more than one place that has to know
what a feature is — the trainer that fits on it, the scorer that reproduces it,
and the test that checks it is not cheating — and if any two of them disagreed
the model would be silently wrong rather than loudly broken. So the definition
lives here once and nothing below it knows what a model looks like.

Nothing in this module may import lightgbm or numpy: it has to stay readable,
and testable, on an install that never took the `ml` extra.
"""

from __future__ import annotations

from dataclasses import dataclass

# p25/p50/p75. Started at p10/p50/p90, but an 80%-coverage band routinely spanned
# 20-30 minutes on erratic stops — technically honest, useless to a passenger
# deciding when to leave. The interquartile band trades coverage guarantee (50%,
# down from 80%) for a range someone can actually act on; conformal calibration
# still makes that 50% a real guarantee rather than a hopeful one.
QUANTILES = (0.25, 0.5, 0.75)

# Facts that exist only because the run already happened. Using any of them
# would produce a model that scores beautifully and cannot predict anything,
# because at prediction time none of them exist yet.
#
# changed_platform is the subtle one and the reason this list is checked rather
# than eyeballed: it is a *planned* field, so it reads as safe, but IRIS only
# publishes it in the last hour before departure and a platform change is itself
# a symptom of disruption. For an a-priori model it is a label wearing a
# schedule's clothes.
LEAKAGE_COLUMNS = frozenset({
    "arr_delay_s",
    "dep_delay_s",
    "actual_arrival_utc",
    "actual_departure_utc",
    "quality",
    "status",
    "cancelled",
    "arr_cancelled",
    "dep_cancelled",
    "cancelled_at_utc",
    "final_delay_s",
    "max_delay_s",
    "n_stops_observed",
    "n_polls_seen",
    "last_observed_at",
    "read_lag_s",
    "poll_gap_s",
    "finalized_at",
    "changed_platform",
})


@dataclass(frozen=True, slots=True)
class Feature:
    """One column the model sees, and the fact it is derived from.

    `source` is what the leakage test checks, so it names the underlying fact
    rather than the arithmetic: "sched_sec" and not "hour of day".
    """

    name: str
    source: str
    categorical: bool = False


# Order is part of the contract: a booster indexes its features positionally, so
# a saved model and the scorer that loads it must build the row the same way.
# The manifest records this list, and loading refuses on a mismatch.
#
# Every feature here is a function of the prediction key
# (day_type, train_id, station_id, sched_sec) plus fixed attributes of that train
# and station. That is not a coincidence: ml/live.py's predict_stop() is only ever
# called with exactly that key — a caller resolves a calendar date down to a
# day_type before it gets here — so a feature the key does not determine could be
# fitted but never scored at request time. It is what keeps weekday-within-WEEKDAY
# and calendar date out of v1; widening either means widening this key first.
FEATURE_SPEC: tuple[Feature, ...] = (
    Feature("day_type", "day_type", categorical=True),
    Feature("train_id", "train_id", categorical=True),
    Feature("station_id", "station_id", categorical=True),
    Feature("line_id", "train.line_id", categorical=True),
    Feature("category", "train.category", categorical=True),
    Feature("operator", "train.operator", categorical=True),
    Feature("segment", "train.segment", categorical=True),
    Feature("platform", "station_stop.platform_code", categorical=True),
    Feature("sched_sec", "sched_sec"),
    Feature("sched_hour", "sched_sec"),
    Feature("sched_minute_of_hour", "sched_sec"),
    Feature("stop_sequence", "station_stop.stop_sequence"),
    Feature("route_position", "station_stop.stop_sequence"),
    Feature("n_stops", "train.n_stops"),
    Feature("first_departure_sec", "train.first_departure_sec"),
    Feature("station_lat", "station.lat"),
    Feature("station_lon", "station.lon"),
)

FEATURE_NAMES = tuple(f.name for f in FEATURE_SPEC)
CATEGORICAL_NAMES = tuple(f.name for f in FEATURE_SPEC if f.categorical)
CATEGORICAL_INDEXES = tuple(i for i, f in enumerate(FEATURE_SPEC) if f.categorical)


def pinball_loss(actual: list[float], predicted: list[float], quantile: float) -> float:
    """Mean pinball loss — the thing a quantile model is actually optimising.

    MAE would score a p90 model as terrible for the correct behaviour of sitting
    above most observations, so it cannot be the yardstick for two of the three
    models here.
    """
    if not actual:
        return 0.0
    total = 0.0
    for a, p in zip(actual, predicted):
        diff = a - p
        total += quantile * diff if diff >= 0 else (quantile - 1) * diff
    return total / len(actual)


def mae(actual: list[float], predicted: list[float]) -> float:
    if not actual:
        return 0.0
    return sum(abs(a - p) for a, p in zip(actual, predicted)) / len(actual)


def coverage(actual: list[float], lo: list[float], hi: list[float]) -> float:
    """Share of observations that land inside the band. The number conformal
    calibration exists to make true rather than hoped for."""
    if not actual:
        return 0.0
    return sum(1 for a, low, high in zip(actual, lo, hi) if low <= a <= high) / len(actual)


def mean_width(lo: list[float], hi: list[float]) -> float:
    """Average band width. Coverage without width is meaningless — a band from
    minus infinity to infinity covers everything and says nothing."""
    if not lo:
        return 0.0
    return sum(high - low for low, high in zip(lo, hi)) / len(lo)
