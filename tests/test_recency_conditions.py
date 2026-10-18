"""Experiment conditions: amplitude/trial mapping, year-free time-of-year inputs, condition lists."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import recency_experiment as experiment  # noqa: E402
from recency_weights import policy, weights  # noqa: E402
from dbrel.ml.arrival import fit_input_prep  # noqa: E402
from dbrel.ml.features import (CATEGORICAL, NOTICE_NUMERIC, NOTICE_RATES, NUMERIC,  # noqa: E402
                               day_of_year_indices, numeric_features)

TRIALS = "data/ml/full_year/runs/compact-2026-09-26/trials/"


def epoch(*dates):
    return np.array(dates, dtype="datetime64[D]").astype(np.int64)


def test_conditions_map_to_amplitude_and_input_trial():
    assert {name: (amplitude, str(trial)) for name, (amplitude, trial) in experiment.CONDITIONS.items()} == {
        "control": (0.0, TRIALS + "f1ac12b1742e3a0b"), "weighted": (0.5, TRIALS + "f1ac12b1742e3a0b"),
        "strong": (2.0, TRIALS + "f1ac12b1742e3a0b"), "seasonal": (0.0, TRIALS + "634b5e057ab5fe00")}
    # Strong: the last training day counts 3x, the month before it 1x.
    record = policy("2026-08-21", experiment.CONDITIONS["strong"][0])
    np.testing.assert_allclose(weights(epoch("2026-06-01", "2026-07-21", "2026-08-21"), record), [1, 1, 3])


def test_seasonal_columns_ignore_the_year():
    pytest.importorskip("torch")
    import train_recency_delay as runner
    prep = {"stats": {"doy_sin": [0.0, 1.0], "doy_cos": [0.0, 1.0]}}
    values = runner.seasonal_inputs(epoch("2025-10-14", "2026-10-14", "2025-03-01", "2026-03-01",
                                          "2025-12-31", "2026-01-01"), prep)
    np.testing.assert_array_equal(values[0], values[1])
    np.testing.assert_array_equal(values[2], values[3])
    assert not np.array_equal(values[0], values[2])
    # 31 Dec and 1 Jan are neighbours on the circle.
    assert np.abs(values[4]-values[5]).max() < 0.05


def _layouts(n=40):
    rng = np.random.default_rng(3)
    days = np.sort(rng.integers(epoch("2025-11-03")[0], epoch("2026-09-20")[0], n))
    raw = {"is_holiday": rng.integers(0, 2, n), "sched_local_sec": rng.integers(0, 90000, n),
           "trip_start_sec": rng.integers(0, 86400, n), "stop_rank": rng.integers(0, 10, n).astype(float),
           "n_stops": np.full(n, 12.0), "lat": rng.uniform(47, 55, n), "lon": rng.uniform(6, 15, n),
           "day_of_year_idx": day_of_year_indices(days)}
    encoded = numeric_features(raw)
    for name in NOTICE_RATES:
        encoded[name] = np.where(np.arange(n) % 7 == 0, np.nan, rng.uniform(0, 1, n))
    for name in CATEGORICAL:
        encoded[name] = rng.integers(0, 3, n)
    vocab = {name: [0, 1, 2] for name in CATEGORICAL}
    mask = np.ones(n, dtype=bool)
    base = fit_input_prep(encoded, mask, vocab, "robust", NUMERIC + NOTICE_RATES)
    seasonal = fit_input_prep(encoded, mask, vocab, "robust", NOTICE_NUMERIC)
    return days, encoded, base, seasonal


def test_seasonal_layout_refuses_any_other_input_difference():
    pytest.importorskip("torch")
    import train_recency_delay as runner
    _, _, base, seasonal = _layouts()
    changed = json.loads(json.dumps(seasonal))
    changed["stats"]["lat"][0] += 1
    with pytest.raises(ValueError, match="beyond doy"):
        runner.seasonal_positions(base, changed)
    with pytest.raises(ValueError, match="beyond doy"):
        runner.seasonal_positions(base, base)


def test_summary_compares_each_variant_with_control(tmp_path):
    import summarize_recency_training as summary
    assert summary.TripDateAccumulator(tmp_path / "default.sqlite").conditions == ("control", "weighted")
    accumulator = summary.TripDateAccumulator(tmp_path / "trips.sqlite", ("control", "strong", "seasonal"))
    target = np.array([0.0, 10.0, 0.0])
    points = {"control": np.array([0.0, 0.0, 10.0]), "strong": np.array([0.0, 9.0, 0.0]),
              "seasonal": np.array([9.0, 0.0, 10.0])}
    accumulator.add(np.array(["t1", "t1", "t2"]), target,
                    {condition: {model: value for model in accumulator.MODELS} for condition, value in points.items()})
    metrics = accumulator.result()["fine"]["metrics"]["within_5_min_displayed"]
    accumulator.close()
    assert metrics["strong_minus_control_mean"] > 0 > metrics["seasonal_minus_control_mean"]

    def by_day(y, p):
        score = summary.Score()
        score.add(np.asarray(y, dtype=float), np.asarray(p, dtype=float))
        return {view: {"d1": score.sufficient(), "d2": score.sufficient()} for view in ("raw", "displayed")}
    boot = summary.paired_day_bootstrap(by_day([0, 10], [0, 0]), by_day([0, 10], [0, 9]), replicates=10, variant="strong")
    assert boot["strong_minus_control"]["raw"]["within_5_min"]["strong_minus_control"] == 0.5
