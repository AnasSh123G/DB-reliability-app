"""What the app does today, and the dumbest reasonable guess, as yardsticks.

**Averages baseline.** Groups *training-day* rows by the stored averages' key:
(station, line, direction, trip start, scheduled time, holiday) for a train
with a line, and (train, station, trip start, scheduled time, holiday) for one
without, as `derive/stop_averages.py` keys `stop_delay_average` and
`train_stop_delay_average`. The stored tables themselves are never read: they
were built from every day, test days included, so scoring them on test days
would be scoring them on their own inputs. Each group gives its empirical
p25/p50/p75 so the baseline has the same band shape as the models. A key with
no training rows is not covered, and gets no value -- nothing is invented.

**Global baseline.** The training days' overall p25/p50/p75 for the side,
applied to every row.
"""

from __future__ import annotations

import numpy as np

from dbrel.ml.features import NO_LINE
from dbrel.ml.metrics import QUANTILES


def _key_columns(features: dict[str, np.ndarray], no_line_code: int) -> np.ndarray:
    """int64 (n, 7) key matrix; line-less rows use train, line rows use line+direction."""
    lineless = features["line"] == no_line_code
    start = np.nan_to_num(features["trip_start_min"], nan=-1).astype(np.int64)
    return np.column_stack([
        lineless.astype(np.int64),
        features["station"].astype(np.int64),
        np.where(lineless, features["train"], features["line"]).astype(np.int64),
        np.where(lineless, -1, features["direction"]).astype(np.int64),
        start,
        features["sched_min"].astype(np.int64),
        features["is_holiday"].astype(np.int64),
    ])


def no_line_code(vocab: dict[str, list]) -> int:
    """The encoded value of "no line" in `vocab`, or -2 if no training row lacked one."""
    try:
        return vocab["line"].index(NO_LINE)
    except ValueError:
        return -2


def _group_quantiles(sorted_y: np.ndarray, starts: np.ndarray, sizes: np.ndarray) -> np.ndarray:
    """Linear-interpolated quantiles per contiguous sorted group (numpy's default method)."""
    out = np.empty((len(starts), len(QUANTILES)), dtype=np.float64)
    for j, alpha in enumerate(QUANTILES):
        pos = alpha * (sizes - 1)
        lo = np.floor(pos).astype(np.int64)
        hi = np.ceil(pos).astype(np.int64)
        frac = pos - lo
        out[:, j] = sorted_y[starts + lo] * (1 - frac) + sorted_y[starts + hi] * frac
    return out


def averages_baseline(
    features: dict[str, np.ndarray],
    y: np.ndarray,
    train_rows: np.ndarray,
    eval_rows: np.ndarray,
    no_line: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Predict eval rows from training-row groups. Returns (pred (m, 3), covered (m,)).

    `train_rows` / `eval_rows` are integer row indices; `y` must be non-NaN on
    both. Uncovered rows' predictions are NaN.
    """
    keys = _key_columns(features, no_line)
    both = np.concatenate([train_rows, eval_rows])
    _, inverse = np.unique(keys[both], axis=0, return_inverse=True)
    inverse = inverse.ravel()
    train_key = inverse[: len(train_rows)]
    eval_key = inverse[len(train_rows) :]

    y_train = y[train_rows].astype(np.float64)
    order = np.lexsort((y_train, train_key))
    sorted_key = train_key[order]
    sorted_y = y_train[order]
    group_keys, starts, sizes = np.unique(sorted_key, return_index=True, return_counts=True)
    group_q = _group_quantiles(sorted_y, starts, sizes)

    pos = np.searchsorted(group_keys, eval_key)
    pos_clipped = np.minimum(pos, len(group_keys) - 1)
    covered = group_keys[pos_clipped] == eval_key if len(group_keys) else np.zeros(len(eval_key), bool)
    pred = np.full((len(eval_rows), len(QUANTILES)), np.nan)
    pred[covered] = group_q[pos_clipped[covered]]
    return pred, covered


def global_baseline(y_train: np.ndarray, n_eval: int) -> np.ndarray:
    q = np.quantile(y_train.astype(np.float64), QUANTILES)
    return np.tile(q, (n_eval, 1))
