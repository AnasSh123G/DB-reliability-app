"""How candidates and baselines are scored.

All figures are in minutes. A predictor that cannot score a row (the averages
baseline, on a key it never saw) marks it not covered; its error figures are
over the rows it did cover, and `covered_share` says how many that was.
Pinball loss is the quantile regression loss: for level alpha it charges
alpha * under-prediction and (1 - alpha) * over-prediction, so a well-placed
25th percentile is under the truth about a quarter of the time.
"""

from __future__ import annotations

import numpy as np

QUANTILES = (0.25, 0.5, 0.75)


def pinball(y: np.ndarray, q: np.ndarray, alpha: float) -> float:
    diff = y - q
    return float(np.mean(np.maximum(alpha * diff, (alpha - 1) * diff)))


def order_quantiles(pred: np.ndarray) -> tuple[np.ndarray, float]:
    """Sort each row's (p25, p50, p75); also return the share of rows that were crossed."""
    crossed = np.any(np.diff(pred, axis=1) < 0, axis=1)
    return np.sort(pred, axis=1), float(np.mean(crossed)) if len(pred) else 0.0


def score(y: np.ndarray, pred: np.ndarray, covered: np.ndarray | None = None) -> dict:
    """Metrics for predictions `pred` (n, 3: p25/p50/p75) of targets `y` (n,)."""
    n = len(y)
    if covered is None:
        covered = np.ones(n, dtype=bool)
    y_c, p_c = y[covered], pred[covered]
    out = {"n": int(n), "covered_share": float(np.mean(covered)) if n else 0.0}
    if len(y_c) == 0:
        out.update({"mae_p50": None, "pinball": None, "mean_pinball": None, "band_coverage": None})
        return out
    losses = {str(a): pinball(y_c, p_c[:, i], a) for i, a in enumerate(QUANTILES)}
    out.update({
        "mae_p50": float(np.mean(np.abs(y_c - p_c[:, 1]))),
        "pinball": losses,
        "mean_pinball": float(np.mean(list(losses.values()))),
        "band_coverage": float(np.mean((y_c >= p_c[:, 0]) & (y_c <= p_c[:, 2]))),
    })
    return out


def score_with_breakdowns(
    y: np.ndarray,
    pred: np.ndarray,
    groups: dict[str, np.ndarray],
    covered: np.ndarray | None = None,
    subsets: dict[str, np.ndarray] | None = None,
) -> dict:
    """`score` overall, plus per value of each grouping array and on named row subsets."""
    if covered is None:
        covered = np.ones(len(y), dtype=bool)
    out = {"overall": score(y, pred, covered)}
    for name, values in groups.items():
        out[f"by_{name}"] = {
            str(value): score(y[values == value], pred[values == value], covered[values == value])
            for value in np.unique(values)
        }
    for name, mask in (subsets or {}).items():
        out[name] = score(y[mask], pred[mask], covered[mask])
    return out
