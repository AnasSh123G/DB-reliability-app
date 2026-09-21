"""Scoring the model against the thing it has to beat.

The baseline here is not a formality. The literature on train delay prediction
is consistent that a historical per-service average is a strong baseline and
hard to improve on by much, and this project already ships one in
train_stop_average. A model that does not beat it is a 56 MB dependency and a
training step in exchange for a worse answer, so the comparison is computed on
every run and gates activation.

The baseline is **rebuilt here from the fit dates only**, never read from the
live train_stop_average table. That table is computed over every date including
the test period, so using it would be comparing an honest model against a
baseline that has seen the answers — and the model would lose to a cheat.

No lightgbm and no numpy: this takes predictions as plain lists, so it can be
tested without the `ml` extra.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from dbrel.derive._common import percentile
from dbrel.ml._common import coverage, mae, mean_width, pinball_loss
from dbrel.ml.features import Dataset


@dataclass(slots=True)
class Evaluation:
    n: int
    model_mae: float
    model_pinball: float
    baseline_mae: float
    baseline_pinball: float
    naive_mae: float
    band_coverage: float
    band_width: float
    cold_start_share: float

    @property
    def improvement(self) -> float:
        """Relative gain over the historical baseline on p50 pinball loss.

        Positive means better. Pinball rather than MAE because it is what the
        median model was fitted on, and because it stays meaningful if the
        target ever moves off the median.
        """
        if self.baseline_pinball == 0:
            return 0.0
        return (self.baseline_pinball - self.model_pinball) / self.baseline_pinball


def historical_baseline(data: Dataset, fit_indexes: list[int]) -> dict[tuple, float]:
    """train_stop_average's answer, rebuilt from the fit period alone.

    Same key and same statistic as the shipped aggregate — median delay per
    (day_type, train, station, scheduled time) — via the same percentile helper,
    so this really is the number the app would have shown.
    """
    buckets: dict[tuple, list[float]] = defaultdict(list)
    for index in fit_indexes:
        buckets[data.keys[index]].append(data.labels[index])
    return {
        key: float(percentile(sorted(int(v) for v in values), 0.5) or 0)
        for key, values in buckets.items()
    }


def baseline_predictions(
    data: Dataset, indexes: list[int], lookup: dict[tuple, float], fallback: float
) -> tuple[list[float], float]:
    """Baseline prediction per row, plus the share with no history to draw on.

    A key the fit period never saw is a cold start — a service whose usual
    lateness is simply unknown. The aggregate can only shrug at those, and the
    share of them is where a model has a genuine structural advantage, so it is
    reported rather than hidden.
    """
    predictions, cold = [], 0
    for index in indexes:
        value = lookup.get(data.keys[index])
        if value is None:
            cold += 1
            value = fallback
        predictions.append(value)
    return predictions, (cold / len(indexes) if indexes else 0.0)


def evaluate(
    actual: list[float],
    model_p50: list[float],
    band_lo: list[float],
    band_hi: list[float],
    baseline: list[float],
    naive: float,
    cold_start_share: float,
) -> Evaluation:
    return Evaluation(
        n=len(actual),
        model_mae=mae(actual, model_p50),
        model_pinball=pinball_loss(actual, model_p50, 0.5),
        baseline_mae=mae(actual, baseline),
        baseline_pinball=pinball_loss(actual, baseline, 0.5),
        naive_mae=mae(actual, [naive] * len(actual)),
        band_coverage=coverage(actual, band_lo, band_hi),
        band_width=mean_width(band_lo, band_hi),
        cold_start_share=cold_start_share,
    )
