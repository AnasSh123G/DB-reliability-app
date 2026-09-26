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
from collections.abc import Collection

import numpy as np

from dbrel.ml.features import CATEGORICAL, UNKNOWN

TRAIN_SHARE = 0.67
VALIDATION_SHARE = 0.13
STUB_DAY_SHARE = 0.25
MIN_FRESH_DAYS = 3


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


def reserve_fresh_holdout(rows_per_date: dict[str, int], *,
                          training: Collection[str], validation: Collection[str],
                          inspected: Collection[str], minimum_days: int = MIN_FRESH_DAYS) -> list[str]:
    """Reserve complete, later and uninspected dates without reading their outcomes."""
    if minimum_days < 1 or not training or not validation:
        raise ValueError("holdout needs training, validation and at least one fresh date")
    train, valid, seen = set(training), set(validation), set(inspected)
    if train & valid or max(train) >= min(valid):
        raise ValueError("training and validation dates must be disjoint and chronological")
    if (train | valid) - rows_per_date.keys():
        raise ValueError("training or validation date is absent from the dataset")
    floor = STUB_DAY_SHARE * float(np.median(list(rows_per_date.values())))
    after = max(train | valid | seen)
    fresh = sorted(d for d, n in rows_per_date.items() if d > after and n >= floor)
    if len(fresh) < minimum_days:
        raise ValueError(f"need {minimum_days} complete uninspected dates after {after}; "
                         f"only {len(fresh)} available")
    return fresh[:minimum_days]


def training_history_before(
    train_mask: np.ndarray, evaluation_cutoffs_utc: np.ndarray, finalized_at: np.ndarray
) -> np.ndarray:
    """Keep only fully finalized training runs before *every* evaluated cutoff.

    Use the earliest evaluation cutoff (not the service date): a run just after
    midnight can cross a date boundary. Unknown finalization times are excluded.
    Fit vocabularies and statistics from this returned mask, not train_mask.
    """
    if len(train_mask) != len(finalized_at) or not len(evaluation_cutoffs_utc):
        raise ValueError("training mask, finalization times and evaluation cutoffs are required")
    if not np.all(np.isfinite(evaluation_cutoffs_utc)):
        raise ValueError("evaluation cutoffs must be finite UTC seconds")
    mask = train_mask & (finalized_at > 0) & (finalized_at <= np.min(evaluation_cutoffs_utc))
    if not mask.any():
        raise ValueError("no finalized training observations before evaluation cutoffs")
    return mask


def arrival_experiment_masks(
    ds, *, training: Collection[str], validation: Collection[str],
    inspected: Collection[str], final_test: bool = False
) -> tuple[DateSplit, dict[str, np.ndarray]]:
    """Fit on observations finalized before validation, with a sealed later test."""
    if ds.cutoff_utc is None or ds.finalized_at is None:
        raise ValueError("arrival experiment requires per-call cutoffs and finalization times")
    if ds.meta.get("timetable_asof_verified") is not False:
        raise ValueError("arrival replay must disclose unverified timetable provenance")
    train, val = sorted(set(training)), sorted(set(validation))
    if not train or not val or max(train) >= min(val) or set(train) & set(val):
        raise ValueError("training and validation dates must be ordered and disjoint")
    dates = ds.meta["dates"]
    if (set(train) | set(val)) - set(dates):
        raise ValueError("requested training or validation date is absent")
    test = reserve_fresh_holdout(
        ds.meta["rows_per_date"], training=train, validation=val, inspected=inspected,
    ) if final_test else []
    split = DateSplit(train=train, validation=val, test=test)
    masks = masks_for(ds.day, dates, split)
    masks["train"] = training_history_before(
        masks["train"], ds.cutoff_utc[masks["validation"]], ds.finalized_at,
    )
    return split, masks


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
