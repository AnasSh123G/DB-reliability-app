from datetime import date
from types import SimpleNamespace
import numpy as np
import pytest
from dbrel.planner.forecast import FixedForecaster, delay_s, expected_departure


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
    "value,seconds", [(-2, 0), (4, 240), (float("nan"), 0), (45, 2700)]
)
def test_delay(value, seconds):
    assert delay_s(value) == seconds


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


def test_model_cache_isolated_by_timetable_generation_and_stamp(monkeypatch, tmp_path):
    from copy import copy
    from planner_fixture import make_timetable
    from dbrel.ml.predict import Prediction
    import dbrel.planner.forecast as module

    calls = []

    class Model:
        def __init__(self, path, *, inference_threads):
            assert inference_threads == 1
            self.offset = int(path.name)

        def predict_calls(self, session, batch, **kwargs):
            calls.extend(batch)
            return [Prediction(c, (c.train.id + self.offset,) * 3, None) for c in batch]

    class Session:
        def get(self, cls, identifier):
            return SimpleNamespace(id=identifier, first_departure_sec=28800)

    monkeypatch.setattr(module, "DelayModel", Model)
    module._MODELS.clear()
    module._DIRECTIONS.clear()
    module._FORECAST_CACHE.clear()
    try:
        first = make_timetable()
        second = copy(first)
        second.var_train = first.var_train.copy()
        second.var_train[0] = 999
        second.var_len = first.var_len.copy()
        second.var_len[0] = 2
        second.var_cut = first.var_cut.copy()
        second.var_cut[0] = 2
        key = (0, date(2026, 10, 19))
        fc = module.ModelForecaster(first, Session(), tmp_path / "0", "isolation-a")
        original = fc.forecast([key])[key]
        other = module.ModelForecaster(second, Session(), tmp_path / "0", "isolation-a")
        isolated = other.forecast([key])[key]
        assert fc.direction is not other.direction
        assert original.shape == (3, 3)
        assert isolated.shape == (2, 3)
        assert isolated[1, 1] == 999
        before = len(calls)
        assert (
            module.ModelForecaster(
                first, Session(), tmp_path / "0", "isolation-a"
            ).forecast([key])[key]
            is original
        )
        assert len(calls) == before
        assert (
            module.ModelForecaster(
                first, Session(), tmp_path / "0", "isolation-a"
            ).direction
            is fc.direction
        )
        updated = module.ModelForecaster(
            first, Session(), tmp_path / "1", "isolation-b"
        ).forecast([key])[key]
        assert set(module._DIRECTIONS) == {(first, "isolation-b")}
        assert module._DIRECTIONS[(first, "isolation-b")] is not fc.direction
        assert updated[1, 1] == original[1, 1] + 1
        assert len(calls) > before
    finally:
        module._MODELS.clear()
        module._DIRECTIONS.clear()
        module._FORECAST_CACHE.clear()


def test_planner_direction_matches_reference_and_queries_category_once(tmp_path):
    from ml_fixture import build
    from sqlalchemy import event, select
    from sqlalchemy.orm import Session
    from dbrel.db import make_engine
    from dbrel.models import Train
    from dbrel.ml.features import direction_for
    from dbrel.planner.forecast import _PlannerDirectionCache

    db = tmp_path / "directions.sqlite"
    build(db)
    engine = make_engine(db)
    paths = ["A|B|C", "C|B|A", "X|Y", "Y|X", "A||B|||C", "B|A", "", None, "A", "C|B|A"]
    with Session(engine) as session:
        session.add_all(
            [
                Train(
                    id=200 + i,
                    identity_key=f"direction-{i}",
                    category="IC",
                    route_path=path,
                    origin_name="A",
                    dest_name="C",
                )
                for i, path in enumerate(paths)
            ]
        )
        session.commit()
        trains = list(session.scalars(select(Train)))
        expected = {t.id: direction_for(session, t) for t in trains}
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        cache = _PlannerDirectionCache()
        assert {t.id: cache.get(session, t) for t in trains} == expected
        assert sum("train.line_id IS NULL" in sql for sql in statements) == 2
        statements.clear()
        assert {t.id: cache.get(session, t) for t in trains} == expected
        assert not statements
        event.remove(engine, "before_cursor_execute", record)
    # Cached scalar snapshots survive session closure without retaining ORM rows.
    with Session(engine) as session:
        trains = list(session.scalars(select(Train)))
        assert {t.id: cache.get(session, t) for t in trains} == expected


def test_planner_direction_snapshot_reuses_station_strings():
    from dbrel.planner.forecast import _PlannerDirectionCache

    class Session:
        def execute(self, query):
            return iter([
                (1, "Northern Station|Central Station|Southern Station"),
                (2, "Southern Station|Central Station|Northern Station"),
                (3, "Northern Station||Central Station|||Southern Station"),
            ])

    train = SimpleNamespace(id=1, category="ICE", line_id=None,
                            route_path="Northern Station|Central Station|Southern Station")
    cache = _PlannerDirectionCache()
    assert cache.get(Session(), train) == 0
    names = [name for row in cache.categories["ICE"] for name in row.names]
    assert len({id(name) for name in names}) == len(set(names))
    assert cache.categories["ICE"][0].raw_length == 6


def test_planner_direction_snapshot_reuses_routes_preserving_raw_length():
    from dbrel.planner.forecast import _PlannerDirectionCache

    class Session:
        def execute(self, query):
            return iter([(1, "North|Middle|South"), (2, "North|Middle|South"),
                         (3, "North||Middle|||South"), (4, "South|Middle|North")])

    train = SimpleNamespace(id=1, category="ICE", line_id=None,
                            route_path="North|Middle|South")
    cache = _PlannerDirectionCache()
    assert cache.get(Session(), train) == 0
    rows = {row.train_id: row for row in cache.categories["ICE"]}
    assert rows[1].names is rows[2].names is rows[3].names
    assert rows[1].stations is rows[2].stations is rows[3].stations
    assert rows[1].raw_length == rows[2].raw_length == 3
    assert rows[3].raw_length == 6
    assert rows[4].names == ("South", "Middle", "North")
    assert cache.categories["ICE"][0].train_id == 3


@pytest.mark.parametrize(
    ("slack_min", "expected"),
    [(10, "low"), (6, "medium"), (3, "high"), (1, "very_high")],
)
def test_miss_chance_places_slack_between_quantiles(slack_min, expected):
    from dbrel.planner.respond import miss_chance

    assert miss_chance(np.array([2.0, 5.0, 8.0]), slack_min * 60) == expected


def test_miss_chance_is_none_without_forecast_and_worst_change_wins():
    from dbrel.planner.respond import miss_chance, worst_miss_chance

    assert miss_chance(np.array([np.nan] * 3), 600) is None
    changes = [{"miss_chance": "low"}, {"miss_chance": None}, {"miss_chance": "high"}]
    assert worst_miss_chance(changes) == "high"
    assert worst_miss_chance([]) is None
