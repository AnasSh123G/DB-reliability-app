"""Rolling date split, inspected dates, and train pruning in the training vocabulary."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from dbrel.ml.arrival import arrival_inputs, fit_input_prep
from dbrel.ml.features import ARRIVAL_NUMERIC, CATEGORICAL, UNKNOWN, encode_categoricals
from dbrel.ml.split import (
    TrainPruning, load_inspected, record_inspected, restrict_to_training_vocab, rolling_split,
    training_vocabulary,
)


def _days(start: str, n: int, rows: int = 1000) -> dict[str, int]:
    first = date.fromisoformat(start)
    return {(first + timedelta(days=i)).isoformat(): rows for i in range(n)}


def test_rolling_split_windows_and_moving_forward():
    rows = _days("2025-11-01", 130)
    rows["2025-11-01"] = 10  # a stub first day belongs to no set
    split = rolling_split(rows, [])
    assert len(split.test) == 7 and len(split.validation) == 28 and len(split.train) == 130 - 1 - 35
    assert "2025-11-01" not in split.train
    assert max(split.train) < min(split.validation) <= max(split.validation) < min(split.test)
    everything = split.train + split.validation + split.test
    assert len(set(everything)) == len(everything)  # a date (and so a run) is in exactly one set

    later = rolling_split({**rows, **_days("2026-03-11", 3)}, [])
    assert later.test[-1] == "2026-03-13" and later.test[0] > split.test[0]
    assert later.validation[0] > split.validation[0]


def test_inspected_dates_never_become_test_dates(tmp_path):
    rows = _days("2025-11-01", 130)
    path = tmp_path / "inspected_dates.json"
    assert load_inspected(path) == []
    first = rolling_split(rows, load_inspected(path))
    record_inspected(path, first.test, "final test of run A")
    with pytest.raises(ValueError, match=r"need 7 complete uninspected dates after 2026-03-10.*have 0"):
        rolling_split(rows, load_inspected(path))
    rows.update(_days("2026-03-11", 7))
    second = rolling_split(rows, load_inspected(path))
    assert not set(second.test) & set(first.test)
    assert set(first.test) <= set(second.validation)  # inspected dates may still be used to validate
    record_inspected(path, second.test, "final test of run B")
    assert load_inspected(path) == sorted(first.test + second.test)


def test_too_little_data_names_available_and_required_dates():
    with pytest.raises(ValueError, match=r"need 118 complete dates .*28 validation \+ 90 training.*have 40"):
        rolling_split(_days("2026-08-01", 47), [])


def _train_rows():
    """Train 0: frequent and recent. 1: rare. 2: frequent but retired 70 days before the end."""
    day0 = int(np.datetime64("2026-01-01", "D").astype(np.int64))
    trains, days = [], []
    for i in range(100):
        trains.append(0)
        days.append(day0 + i)
    for i in range(5):
        trains.append(1)
        days.append(day0 + 90 + i)
    for i in range(25):
        trains.append(2)
        days.append(day0 + i)
    return np.array(trains, dtype=np.int32), np.array(days, dtype=np.int64), day0 + 99


def test_train_pruning_drops_rare_and_retired_trains_everywhere():
    trains, days, last = _train_rows()
    n = len(trains)
    features = {c: np.zeros(n, dtype=np.int32) for c in CATEGORICAL}
    features["train"] = trains
    for name in ARRIVAL_NUMERIC:
        features[name] = np.zeros(n, dtype=np.float32)
    vocab = {c: [0] for c in CATEGORICAL}
    vocab["train"] = [700, 701, 702]  # raw ids for codes 0, 1, 2
    mask = np.ones(n, dtype=bool)
    pruning = TrainPruning()
    coded, pruned = restrict_to_training_vocab(features, vocab, mask, epoch_day=days, pruning=pruning)
    assert pruned["train"] == [700]
    assert set(coded["train"][trains != 0].tolist()) == {UNKNOWN}

    # Prediction encodes raw ids through the saved vocabulary: the pruned trains get slot 0.
    raw = {c: np.zeros(3, dtype=np.int64) for c in CATEGORICAL}
    raw["train"] = np.array([700, 701, 702])
    for name in ARRIVAL_NUMERIC:
        raw[name] = np.zeros(3, dtype=np.float32)
    prep = fit_input_prep(coded, mask, pruned, numeric=ARRIVAL_NUMERIC)
    cats, _ = arrival_inputs(encode_categoricals(raw, pruned), prep)
    assert cats[:, CATEGORICAL.index("train")].tolist() == [1, 0, 0]

    # The streamed vocabulary applies the same rule to the same statistics.
    stats = {c: ([0], np.array([n]), np.array([last])) for c in CATEGORICAL}
    stats["train"] = ([700, 701, 702], np.array([100, 5, 25]), np.array([last, last - 5, last - 75]))
    assert training_vocabulary(stats, last, pruning)["train"] == [700]
    assert training_vocabulary(stats, last, TrainPruning(min_count=5, recent_days=80))["train"] == [700, 701, 702]
    assert training_vocabulary(stats, last, None)["train"] == [700, 701, 702]
    with pytest.raises(ValueError, match="epoch_day"):
        restrict_to_training_vocab(features, vocab, mask, pruning=pruning)
