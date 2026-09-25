"""Chronological split by whole service dates, and the training-day vocabulary.

**By date, never by row.** A random split would put the 08:14 stop of a run in
training and its 08:31 stop in test, and the model would score brilliantly by
having already seen that run. Whole dates in time order answer the question the
model exists for: what happens on a day it has not seen. Every stop of a run
shares its trip's service_date, so no run can straddle two sets.

Stub dates (collection starting or stopping mid-day) are dropped: a date with
under a quarter of the median day's rows is not a representative day.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from dbrel.ml.features import CATEGORICAL, UNKNOWN

TRAIN_SHARE = 0.67
VALIDATION_SHARE = 0.13
STUB_DAY_SHARE = 0.25


@dataclass(frozen=True)
class DateSplit:
    train: list[str]
    validation: list[str]
    test: list[str]

    def as_dict(self) -> dict[str, list[str]]:
        return {"train": self.train, "validation": self.validation, "test": self.test}


def split_dates(rows_per_date: dict[str, int]) -> DateSplit:
    """Contiguous train / validation / test ranges of the usable dates, in date order."""
    counts = np.array(list(rows_per_date.values()))
    if len(counts) == 0:
        raise ValueError("no dates to split")
    floor = STUB_DAY_SHARE * float(np.median(counts))
    usable = sorted(d for d, n in rows_per_date.items() if n >= floor)
    if len(usable) < 3:
        raise ValueError(f"need at least 3 full service dates, have {len(usable)}")
    n_train = max(1, round(TRAIN_SHARE * len(usable)))
    n_val = max(1, round(VALIDATION_SHARE * len(usable)))
    n_train = min(n_train, len(usable) - 2)
    n_val = min(n_val, len(usable) - n_train - 1)
    return DateSplit(
        train=usable[:n_train],
        validation=usable[n_train : n_train + n_val],
        test=usable[n_train + n_val :],
    )


def masks_for(day: np.ndarray, dates: list[str], split: DateSplit) -> dict[str, np.ndarray]:
    """Boolean row masks per set, from the dataset's day index."""
    index = {d: i for i, d in enumerate(dates)}
    out = {}
    for name, members in split.as_dict().items():
        wanted = np.zeros(len(dates), dtype=bool)
        wanted[[index[d] for d in members]] = True
        out[name] = wanted[day]
    return out


def restrict_to_training_vocab(
    features: dict[str, np.ndarray], vocab: dict[str, list], train_mask: np.ndarray
) -> tuple[dict[str, np.ndarray], dict[str, list]]:
    """Re-code categoricals so only values seen on training days have a code.

    Anything else becomes UNKNOWN -- a train first running on a test day is as
    new to the model as it would be at prediction time. Codes are re-packed to
    0..k-1, and the returned vocabulary lists the values in that order, which is
    what the saved manifest carries and prediction encodes against.
    """
    out = dict(features)
    new_vocab: dict[str, list] = {}
    for name in CATEGORICAL:
        codes = features[name]
        seen = np.unique(codes[train_mask])
        seen = seen[seen != UNKNOWN]
        lookup = np.full(len(vocab[name]) + 1, UNKNOWN, dtype=np.int32)  # +1: slot for UNKNOWN
        lookup[seen] = np.arange(len(seen), dtype=np.int32)
        out[name] = np.where(codes == UNKNOWN, UNKNOWN, lookup[codes]).astype(np.int32)
        new_vocab[name] = [vocab[name][c] for c in seen.tolist()]
    return out, new_vocab
