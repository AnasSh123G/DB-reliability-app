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

import json
import time
from dataclasses import asdict, dataclass
from collections.abc import Collection
from pathlib import Path

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


def complete_dates(rows_per_date: dict[str, int]) -> list[str]:
    """Dates with at least a quarter of the median day's rows, in order."""
    if not rows_per_date:
        return []
    floor = STUB_DAY_SHARE * float(np.median(list(rows_per_date.values())))
    return sorted(d for d, n in rows_per_date.items() if n >= floor)


def rolling_split(rows_per_date: dict[str, int], inspected: Collection[str], *, test_days: int = 7,
                  val_days: int = 28, min_train_days: int = 90) -> DateSplit:
    """The latest complete dates as a sealed test week, the four weeks before as validation.

    Test dates must all come after every inspected date: a date whose outcomes
    were already looked at can never be a test date again, so a second final
    test needs newer data. Validation may include inspected dates; everything
    earlier is training. Stub dates are in no set.
    """
    usable = complete_dates(rows_per_date)
    seen = set(inspected)
    fresh = [d for d in usable if not seen or d > max(seen)]
    if len(fresh) < test_days:
        raise ValueError(f"need {test_days} complete uninspected dates after "
                         f"{max(seen) if seen else 'the start'} for the test week; have {len(fresh)}")
    test = fresh[-test_days:]
    before = [d for d in usable if d < test[0]]
    needed = val_days + min_train_days
    if len(before) < needed:
        raise ValueError(f"need {needed} complete dates before the test week ({val_days} validation + "
                         f"{min_train_days} training); have {len(before)} ({before[0] if before else '-'}"
                         f" .. {before[-1] if before else '-'})")
    return DateSplit(train=before[:-val_days], validation=before[-val_days:], test=test)


INSPECTED_NAME = "inspected_dates.json"


def load_inspected(path: Path) -> list[str]:
    """Dates whose outcomes a final test has already read (`data/ml/inspected_dates.json`)."""
    return sorted(json.loads(path.read_text())["dates"]) if path.exists() else []


def record_inspected(path: Path, dates: Collection[str], reason: str) -> list[str]:
    """Add `dates` to the inspected list, with a log entry saying why; returns the new list."""
    data = json.loads(path.read_text()) if path.exists() else {"dates": [], "log": []}
    data["dates"] = sorted(set(data["dates"]) | set(dates))
    data["log"].append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "dates": sorted(dates), "reason": reason})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)
    return data["dates"]


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


@dataclass(frozen=True)
class TrainPruning:
    """Which trains keep their own code: enough training arrivals, and recently seen.

    `recent_days` counts back from the last training date, inclusive; a train
    retired by a timetable change ages out that long after its last run.
    """
    min_count: int = 20
    recent_days: int = 56

    def keeps(self, counts: np.ndarray, last_day: np.ndarray, last_training_day: int) -> np.ndarray:
        return (counts >= self.min_count) & (last_day > last_training_day - self.recent_days)

    def as_dict(self) -> dict:
        return asdict(self)


def training_vocabulary(stats: dict[str, tuple[list, np.ndarray, np.ndarray]], last_training_day: int,
                        pruning: TrainPruning | None = None) -> dict[str, list]:
    """Vocabulary from per-value training statistics: {name: (values, counts, last epoch day)}.

    The streamed counterpart of `restrict_to_training_vocab`: every value seen
    in training keeps a code, except trains `pruning` drops. Values come back
    sorted, as `encode_categoricals` needs.
    """
    vocab = {}
    for name in CATEGORICAL:
        values, counts, last_day = stats[name]
        keep = np.ones(len(values), dtype=bool)
        if name == "train" and pruning is not None:
            keep = pruning.keeps(np.asarray(counts), np.asarray(last_day), last_training_day)
        vocab[name] = sorted(v for v, k in zip(values, keep.tolist()) if k)
    return vocab


def restrict_to_training_vocab(
    features: dict[str, np.ndarray], vocab: dict[str, list], train_mask: np.ndarray, *,
    epoch_day: np.ndarray | None = None, pruning: TrainPruning | None = None,
) -> tuple[dict[str, np.ndarray], dict[str, list]]:
    """Re-code categoricals so only values seen on training days have a code.

    Anything else becomes UNKNOWN -- a train first running on a test day is as
    new to the model as it would be at prediction time. Codes are re-packed to
    0..k-1, and the returned vocabulary lists the values in that order, which is
    what the saved manifest carries and prediction encodes against.

    With `pruning` (and each row's `epoch_day`, days since 1970-01-01), rare and
    retired trains become UNKNOWN too.
    """
    if pruning is not None and epoch_day is None:
        raise ValueError("train pruning needs each row's epoch_day")
    out = dict(features)
    new_vocab: dict[str, list] = {}
    for name in CATEGORICAL:
        codes = features[name]
        seen = np.unique(codes[train_mask])
        seen = seen[seen != UNKNOWN]
        if name == "train" and pruning is not None:
            train_codes = codes[train_mask]
            known = train_codes != UNKNOWN
            counts = np.bincount(train_codes[known], minlength=len(vocab[name]))
            last_day = np.full(len(vocab[name]), np.iinfo(np.int64).min, dtype=np.int64)
            np.maximum.at(last_day, train_codes[known], epoch_day[train_mask][known])
            last_training_day = int(epoch_day[train_mask].max())
            seen = seen[pruning.keeps(counts[seen], last_day[seen], last_training_day)]
        lookup = np.full(len(vocab[name]) + 1, UNKNOWN, dtype=np.int32)  # +1: slot for UNKNOWN
        lookup[seen] = np.arange(len(seen), dtype=np.int32)
        out[name] = np.where(codes == UNKNOWN, UNKNOWN, lookup[codes]).astype(np.int32)
        new_vocab[name] = [vocab[name][c] for c in seen.tolist()]
    return out, new_vocab
