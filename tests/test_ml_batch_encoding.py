"""Batch input assembly preserves the legacy timetable feature contract."""
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from dbrel.ml import predict
from dbrel.ml.features import RAW_COLUMNS, CATEGORICAL, compute_features, encode_categoricals
from dbrel.ml.timetable import ScheduledCall

HOLIDAYS = {('2026-12-25', 'DE-NW')}
COLUMNS = ('station_id', 'train_id', 'line_id', 'category', 'operator', 'state',
           'lat', 'lon', 'weekday', 'is_holiday', 'sched_local_sec', 'trip_start_sec',
           'stop_rank', 'n_stops', 'direction', 'day_of_year_idx')


def calls():
    train = SimpleNamespace(id=7, line_id=None, category='ICE', operator='DB')
    station = SimpleNamespace(id=8, state='DE-NW', lat=51.5, lon=7.5)
    return [ScheduledCall(train, station, date(2026, 12, 25), arr, dep, rank, 3, start)
            for arr, dep, rank, start in [(90000, 90120, 1, 85000),
                                          (None, 86000, 0, None),
                                          (92000, None, 2, 85000),
                                          (None, None, 1, None)]]


def legacy_values(call, arrival_clock):
    sched = call.planned_arrival_sec if arrival_clock else (
        call.planned_departure_sec if call.planned_departure_sec is not None
        else call.planned_arrival_sec)
    return (8, 7, -1, 'ICE', 'DB', 'DE-NW', 51.5, 7.5, 4, 1, sched,
            np.nan if call.trip_start_sec is None else call.trip_start_sec,
            call.stop_rank, 3, 1, 359)


@pytest.mark.parametrize('arrival_clock', [False, True])
def test_raw_values_field_order_and_public_wrapper(arrival_clock):
    assert RAW_COLUMNS == COLUMNS
    for call in calls():
        expected = legacy_values(call, arrival_clock)
        values = call.raw_values(direction=1, holiday_set=HOLIDAYS, arrival_clock=arrival_clock)
        raw = call.raw(direction=1, holiday_set=HOLIDAYS, arrival_clock=arrival_clock)
        assert tuple(raw) == COLUMNS
        for name, value, want in zip(COLUMNS, values, expected):
            if isinstance(want, float) and np.isnan(want):
                assert np.isnan(value) and np.isnan(raw[name][0])
            else:
                assert value == want and raw[name] == [want]


@pytest.mark.parametrize('arrival_clock', [False, True])
def test_batch_features_and_predictions_match_legacy(monkeypatch, arrival_clock):
    batch = calls()
    arrays = {name: np.array([legacy_values(c, arrival_clock)[i] for c in batch],
                             dtype=object if name in ('category', 'operator', 'state') else np.float64)
              for i, name in enumerate(COLUMNS)}
    vocab = {name: [] for name in CATEGORICAL}
    expected = encode_categoricals(compute_features(arrays), vocab)
    captured = []

    def fake_predict(features):
        captured.append(features)
        n = len(batch)
        percentiles = np.tile([1., 2., 3.], (n, 1))
        if arrival_clock:
            return SimpleNamespace(percentiles=percentiles, point=np.full(n, 2.),
                                   missing_reason=[''] * n, rule='median')
        return {'arr': percentiles, 'dep': percentiles}

    model = object.__new__(predict.DelayModel)
    model.manifest = {'vocab': vocab}
    model.arrival = SimpleNamespace(predict=fake_predict) if arrival_clock else None
    model.tabm = None if arrival_clock else SimpleNamespace(predict=fake_predict)
    monkeypatch.setattr(predict, 'load_holiday_dates', lambda session: HOLIDAYS)
    monkeypatch.setattr(predict, 'add_serving_rates', lambda features, *args: features)
    directions = []
    result = model.predict_calls(SimpleNamespace(get_bind=lambda: None), batch,
                                 direction=lambda session, train: directions.append(train.id) or 1)
    assert directions == [7]
    for name in expected:
        np.testing.assert_array_equal(captured[0][name], expected[name])
    assert [p.arr for p in result] == [(1., 2., 3.), None, (1., 2., 3.), None]
    assert [p.dep for p in result] == ([None] * 4 if arrival_clock else
                                     [(1., 2., 3.), (1., 2., 3.), None, None])
