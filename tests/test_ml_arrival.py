"""Arrival-only candidates: forecast contract, decision rules, input prep, histogram."""

from __future__ import annotations

import numpy as np
import pytest

from dbrel.ml.arrival import (
    ArrivalForecast, ConditionalHistogram, arrival_inputs, fit_input_prep,
)
from dbrel.ml.features import ARRIVAL_NUMERIC, CATEGORICAL, UNKNOWN


def _encoded(n, **columns):
    out = {c: np.zeros(n, dtype=np.int32) for c in CATEGORICAL}
    for name in ARRIVAL_NUMERIC:
        out[name] = np.zeros(n, dtype=np.float32)
    out["sched_min"] = np.full(n, 8 * 60, dtype=np.float32)
    for name, values in columns.items():
        out[name] = np.asarray(values, dtype=out[name].dtype)
    return out


# --- forecast contract --------------------------------------------------------

def test_forecast_requires_finite_points_or_a_reason():
    ok = ArrivalForecast.build([1.0, 7.0], ["", "no_training_history"], "window")
    assert np.isnan(ok.point[1]) and ok.percentiles is None
    with pytest.raises(ValueError, match="finite"):
        ArrivalForecast(np.array([np.nan], np.float32), np.array([""]), "window")
    with pytest.raises(ValueError, match="NaN point"):
        ArrivalForecast(np.array([2.0], np.float32), np.array(["x"]), "window")
    with pytest.raises(ValueError, match="ordered"):
        ArrivalForecast.build([1.0], [""], "window", [[2.0, 1.0, 3.0]])
    with pytest.raises(ValueError, match="equal p50"):
        ArrivalForecast.build([1.0], [""], "median", [[0.0, 2.0, 3.0]])
    with pytest.raises(ValueError, match="point rule"):
        ArrivalForecast.build([1.0], [""], "p50")


# --- selection and reporting ------------------------------------------------------


# --- decision rules -------------------------------------------------------------


# --- network inputs -------------------------------------------------------------

def test_input_prep_unknowns_and_training_only_statistics():
    vocab = {c: [0, 1] for c in CATEGORICAL}
    enc = _encoded(4, train=[0, 1, UNKNOWN, 7], lat=[1.0, 3.0, np.nan, 1000.0])
    train = np.array([True, True, False, False])
    prep = fit_input_prep(enc, train, vocab, scaling="robust")
    cats, nums = arrival_inputs(enc, prep)
    t = CATEGORICAL.index("train")
    assert prep["cards"][t] == 3
    assert cats[:, t].tolist() == [1, 2, 0, 0]       # UNKNOWN and an out-of-vocab code -> slot 0
    assert prep["stats"]["lat"][0] == 2.0            # median of training rows only
    assert "lat_missing" not in prep["numeric"]      # training rows had no missing lat
    lat = nums[:, prep["numeric"].index("lat")]
    assert lat[2] == 0.0 and abs(lat[3]) < 3.0       # NaN -> 0; smooth clipping bounds outliers
    enc2 = dict(enc, lat=np.array([1.0, 3.0, 50.0, 60.0], np.float32))
    assert fit_input_prep(enc2, train, vocab, "robust")["stats"] == prep["stats"]
    with pytest.raises(ValueError, match="scaling"):
        fit_input_prep(enc, train, vocab, scaling="minmax")


# --- conditional histogram -------------------------------------------------------

def _history():
    """Train 0 at station 0: 12 normal-day arrivals at 1..3 min plus 3 hard cases at 20+
    and 12 holiday arrivals at 8 min; train 1 (same line) has too little history."""
    y = [1, 1, 2, 2, 2, 2, 3, 3, 2, 1, 2, 2, 20, 25, 30] + [8] * 12 + [5, 6]
    n = len(y)
    holiday = [0] * 15 + [1] * 12 + [0, 0]
    train = [0] * 27 + [1, 1]
    return _encoded(n, train=train, is_holiday=holiday), np.array(y, np.float32)


def test_histogram_uses_exact_key_then_backs_off_by_support():
    enc, y = _history()
    model = ConditionalHistogram(min_support=10, point_rule="window").fit(enc, y, np.ones(len(y), bool))
    query = _encoded(4, train=[0, 0, 1, UNKNOWN], is_holiday=[0, 1, 0, 0])
    out = model.predict(query)
    assert out.point.shape == (4,) and out.percentiles.shape == (4, 3)
    assert out.detail["level"].tolist() == ["train_station", "train_station",
                                           "line_station_hour", "line_station_hour"]
    assert out.point[0] == pytest.approx(2.0)          # densest window of train 0, normal days
    assert out.point[1] == pytest.approx(8.0)          # day type kept: holiday history only
    assert out.detail["support"].tolist() == [15, 12, 17, 17]
    assert np.all(np.diff(out.percentiles, axis=1) >= 0)
    assert (out.missing_reason == "").all()


def test_histogram_never_reads_evaluated_outcomes():
    enc, y = _history()
    train = np.arange(len(y)) < 27
    first = ConditionalHistogram(min_support=1).fit(enc, y, train).predict(enc)
    y2 = y.copy()
    y2[~train] = 99.0
    second = ConditionalHistogram(min_support=1).fit(enc, y2, train).predict(enc)
    assert np.array_equal(first.point, second.point)


