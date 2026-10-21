from datetime import date
from types import SimpleNamespace
import numpy as np
import pytest
from dbrel.planner.forecast import FixedForecaster, expected_departure


def tiny():
    return SimpleNamespace(
        var_len=[3],
        var_first=[0],
        var_cut=[3],
        var_train=[100],
        stop_arr=np.array([-2147483648, 30600, 32400]),
        stop_dep=np.array([28800, 30660, -2147483648]),
        stop_si=[0, 1, 2],
        station_id=[1, 2, 3],
    )


def test_fixed_forecaster_shape_and_counters():
    fc = FixedForecaster(tiny(), {(100, 2): (1, 4, 6)})
    key = (0, date(2026, 10, 19))
    rows = fc.forecast([key])[key]
    assert rows.shape == (3, 3)
    assert np.isnan(rows[0]).all()
    assert tuple(rows[1]) == (1, 4, 6)
    assert fc.calls == 1 and fc.runs_forecast == 1 and fc.keys == [key]
    tt = tiny()
    tt.var_cut = [2]
    assert not np.isnan(FixedForecaster(tt, {}).forecast([key])[key][2]).any()


@pytest.mark.parametrize(
    "value,seconds",
    [(4, 240), (0, 0), (float("nan"), 0), (30, 1800), (45, 1800), (-2, 0)],
)
def test_departure_cap(value, seconds):
    tt = tiny()
    arr = np.full((3, 3), value)
    assert expected_departure(tt, arr, 100, 0, 1) == 100 + 30660 + seconds
    assert expected_departure(tt, arr, 100, 0, 0) == 100 + 28800


def test_model_calls_equal_browser_and_lru(tmp_path, monkeypatch):
    from ml_fixture import build
    from sqlalchemy.orm import Session
    from dbrel.db import make_engine
    from dbrel.ml.predict import Prediction
    from dbrel.ml.timetable import calls_at
    from dbrel.planner.timetable import load_timetable
    import dbrel.planner.forecast as module

    db = tmp_path / "fixture.sqlite"
    build(db)
    recorded = []

    class Model:
        def __init__(self, path, *, inference_threads):
            assert inference_threads == 1
            pass

        def predict_calls(self, session, calls, **kwargs):
            recorded.extend(calls)
            return [Prediction(c, (1.0, 2.0, 3.0), None) for c in calls]

    monkeypatch.setattr(module, "DelayModel", Model)
    monkeypatch.setattr(module, "FORECAST_CACHE_RUNS", 2)
    module._MODELS.clear()
    module._DIRECTIONS.clear()
    module._FORECAST_CACHE.clear()
    with Session(make_engine(db)) as session:
        tt = load_timetable(session)
        fc = module.ModelForecaster(tt, session, tmp_path, "test-equivalence")
        keys = [(0, date(2026, 10, d)) for d in (19, 20, 21)]
        result = fc.forecast(keys[:1])
        assert recorded
        for call in recorded:
            browser = calls_at(
                session,
                call.train,
                call.station.id,
                call.service_date,
                variant=tt.var_path[0],
            )
            assert call in browser
        assert np.all(
            result[keys[0]][~np.isnan(result[keys[0]][:, 0])] == np.array([1, 2, 3])
        )
        fc.forecast(keys[1:])
        assert list(module._FORECAST_CACHE) == [
            (tt, "test-equivalence", *k) for k in keys[1:]
        ]
        before = len(recorded)
        fc.forecast([keys[1]])
        assert len(recorded) == before
        assert list(module._FORECAST_CACHE)[-1] == (tt, "test-equivalence", *keys[1])
    module._MODELS.clear()
    module._DIRECTIONS.clear()
    module._FORECAST_CACHE.clear()


@pytest.mark.parametrize(
    ("slack_min", "expected"),
    [(10, "low"), (6, "medium"), (3, "high"), (1, "very_high")],
)
def test_miss_chance_places_slack_between_quantiles(slack_min, expected):
    from dbrel.planner.respond import miss_chance

    assert miss_chance(np.array([2.0, 5.0, 8.0]), slack_min * 60) == expected


