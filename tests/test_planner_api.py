"""Planner HTTP contracts against the tiny synthetic timetable."""

import json
from contextlib import nullcontext
from datetime import datetime
import os

import django
import pytest
from django.test import RequestFactory

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dbrel.web.settings")
django.setup()

from dbrel.config import TZ
from dbrel.web import queries, views
from dbrel.planner import timetable
from dbrel.planner.forecast import FixedForecaster
from planner_fixture import make_timetable


@pytest.fixture(autouse=True)
def planner(monkeypatch):
    tt = make_timetable()
    monkeypatch.setattr(timetable, "get_timetable", lambda session: tt)
    monkeypatch.setattr(views, "read_scope", lambda: nullcontext(None))
    monkeypatch.setattr(
        queries, "_planner_forecaster", lambda session, tt: FixedForecaster(tt, {})
    )
    monkeypatch.setattr(
        queries,
        "_planner_names",
        lambda session: lambda id: (f"RE{id}", f"RE {id}", "RE"),
    )
    return tt


def request(timings=False, **overrides):
    params = {"from": "1", "to": "4", "date": "2026-10-19", "time": "08:00"}
    params.update(overrides)
    response = (views.plan_timings if timings else views.plan)(
        RequestFactory().get("/api/plan", params)
    )
    return response.status_code, json.loads(response.content)


@pytest.mark.parametrize(
    "params", [{"time": ""}, {"from": "1", "to": "1"}, {"from": "abc"}]
)
def test_bad_parameters(params):
    assert request(**params)[0] == 400


def test_bad_route():
    assert request(True, route="bad")[0] == 400


def test_unknown_station():
    assert request(to="12345") == (404, {"error": "no station 12345"})


def test_missing_model(monkeypatch):
    monkeypatch.setattr(queries, "_planner_forecaster", lambda session, tt: None)
    assert request() == (503, {"error": "no delay model is available"})


def test_journey():
    status, body = request()
    assert status == 200
    assert set(body) == {
        "from",
        "to",
        "date",
        "time",
        "model_stamp",
        "data_until",
        "journeys",
        "reason",
        "before",
        "stats",
    }
    # Each connection leaves after the one before and arrives no sooner.
    assert [j["route_key"] for j in body["journeys"]] == ["pl100@1>2~pl200@2>4", "cICE@1>4"]
    departures = [j["departure"]["epoch"] for j in body["journeys"]]
    arrivals = [j["arrival"]["epoch"] for j in body["journeys"]]
    assert departures == sorted(set(departures)) and arrivals == sorted(arrivals)
    first = body["journeys"][0]
    assert first["legs"][0]["departure"] == {
        "epoch": int(datetime(2026, 10, 19, 8, tzinfo=TZ).timestamp()),
        "date": "2026-10-19",
        "time": "08:00",
    }
    assert first["n_changes"] == 1
    assert first["changes"][0]["planned_min"] == first["changes"][0]["left_min"] == 6


def test_before_lists_only_connections_leaving_earlier(monkeypatch):
    # Train 100 (08:00) runs 4 minutes late at B and misses its change, so the 07:30 train 102,
    # which does not, is worth showing among the connections before 08:00.
    monkeypatch.setattr(
        queries,
        "_planner_forecaster",
        lambda session, tt: FixedForecaster(tt, {(100, 2): (4, 4, 4)}),
    )
    status, body = request(before="1")
    assert status == 200 and body["before"] is True
    cutoff = int(datetime(2026, 10, 19, 8, tzinfo=TZ).timestamp())
    assert body["journeys"]
    assert all(j["departure"]["epoch"] < cutoff for j in body["journeys"])
    assert body["journeys"][0]["departure"]["time"] == "07:30"


def test_before_leaves_out_what_a_later_connection_beats():
    # Without delays train 100 (08:00) arrives as soon as 102 (07:30) and leaves later.
    status, body = request(before="1")
    assert status == 200 and body["journeys"] == [] and body["reason"] == "no_connection"


def test_before_with_nothing_earlier_says_no_connection():
    status, body = request(time="07:30", before="1")
    assert status == 200
    assert body["journeys"] == [] and body["reason"] == "no_connection"


def test_change_risk(monkeypatch):
    monkeypatch.setattr(
        queries,
        "_planner_forecaster",
        lambda session, tt: FixedForecaster(tt, {(100, 2): (0, 0, 3)}),
    )
    _, body = request()
    assert body["journeys"][0]["at_risk"] is True
    assert body["journeys"][0]["changes"][0]["left_min"] == 6


@pytest.mark.parametrize(
    "params,reason",
    [
        ({"from": "6"}, "from_unserved"),
        ({"from": "2", "to": "1"}, "to_unserved"),
        ({"to": "3", "time": "10:00"}, "no_connection"),
    ],
)
def test_empty_reason(params, reason, planner):
    if reason == "from_unserved":
        params = {
            "from": str(
                next(
                    int(planner.station_id[i])
                    for i, name in enumerate(planner.station_name)
                    if name == "Z"
                )
            )
        }
    status, body = request(**params)
    assert status == 200
    assert body["reason"] == reason


def test_timings():
    status, body = request(True, route="pl100@1>2~pl200@2>4")
    assert status == 200
    assert body["window"]["from"]["time"] == "06:00"
    assert body["window"]["to"]["time"] == "10:00"


def test_busy_after_validation(monkeypatch):
    class Busy:
        def acquire(self, **kwargs):
            return False

        def release(self):
            raise AssertionError("unacquired slot released")

    monkeypatch.setattr(views, "_PLAN_SLOTS", Busy())
    assert request(to="12345")[0] == 404
    assert request() == (503, {"error": "the planner is busy, try again"})


def test_search_limit(monkeypatch):
    from dbrel.planner.search import Budget

    monkeypatch.setattr(
        queries, "_planner_budget", lambda: Budget(max_runs_per_search=1)
    )
    assert request()[1]["reason"] == "search_limit"


def test_validation_order_and_required_moment(monkeypatch):
    monkeypatch.setattr(queries, "_planner_forecaster", lambda session, tt: None)
    assert request(**{"from": "-1", "time": ""}) == (
        400,
        {"error": "from and to must be station ids"},
    )
    assert request(**{"from": "1", "to": "1", "time": ""}) == (
        400,
        {"error": "from and to must be different stations"},
    )
    assert request(date="", time="") == (400, {"error": "date and time are required"})
    assert request(True, route="bad", to="12345") == (
        400,
        {"error": "route is not a valid route key"},
    )
    assert request(to="12345") == (404, {"error": "no station 12345"})


def test_busy_slot_released_on_success(monkeypatch):
    class Slot:
        released = 0

        def acquire(self, **kwargs):
            return True

        def release(self):
            self.released += 1

    slot = Slot()
    monkeypatch.setattr(views, "_PLAN_SLOTS", slot)
    assert request()[0] == 200
    assert slot.released == 1


def test_response_schema_and_nan(monkeypatch):
    monkeypatch.setattr(
        queries,
        "_planner_forecaster",
        lambda session, tt: FixedForecaster(tt, {}, default=(float("nan"),) * 3),
    )
    status, body = request()
    assert status == 200
    journey = body["journeys"][0]
    assert set(journey) == {
        "route_key",
        "departure",
        "arrival",
        "expected_arrival",
        "delay_min",
        "forecast",
        "duration_min",
        "n_changes",
        "at_risk",
        "miss_chance",
        "legs",
        "changes",
    }
    assert set(journey["legs"][0]) == {
        "train_id",
        "name",
        "number_name",
        "category",
        "line_key",
        "from",
        "to",
        "departure",
        "arrival",
        "expected_arrival",
        "forecast",
        "n_stops",
    }
    assert set(journey["changes"][0]) == {
        "station",
        "planned_min",
        "left_min",
        "at_risk",
        "miss_chance",
    }
    assert journey["forecast"] is None
    assert journey["delay_min"] == 0
    assert set(body["stats"]) == {"searches", "runs_forecast", "ms"}
    _, timings = request(True, route=journey["route_key"])
    assert set(timings) == {
        "from",
        "to",
        "route_key",
        "date",
        "time",
        "model_stamp",
        "data_until",
        "window",
        "instances",
        "stats",
    }


def test_change_departure_cap_does_not_cap_arrival(planner):
    import numpy as np
    from datetime import date
    from dbrel.planner.respond import change_json, journey_json
    from dbrel.planner.search import LegRef
    from planner_fixture import make_late_connection_timetable

    tt = make_late_connection_timetable()
    first = next(i for i, tr in enumerate(tt.var_train) if tr == 100)
    second = next(i for i, tr in enumerate(tt.var_train) if tr == 250)
    day = date(2026, 10, 19)
    legs = [LegRef(first, day, 0, 1), LegRef(second, day, 1, 2)]
    cache = {(first, day): np.zeros((3, 3)), (second, day): np.full((3, 3), 45.0)}
    change = change_json(tt, cache, *legs)
    assert change["left_min"] == 36  # 08:30 to 08:36 + capped 30 minutes.
    body = journey_json(tt, cache, legs, lambda id: ("RE", f"RE {id}", "RE"))
    assert body["delay_min"] == 45
    assert body["expected_arrival"]["time"] == "10:05"


def test_slot_released_when_search_raises(monkeypatch):
    from dbrel.planner import routes

    class Slot:
        released = 0

        def acquire(self, **kwargs):
            return True

        def release(self):
            self.released += 1

    slot = Slot()
    monkeypatch.setattr(views, "_PLAN_SLOTS", slot)

    def fail(*args, **kwargs):
        raise RuntimeError("test search failed")

    monkeypatch.setattr(routes, "plan_routes", fail)
    with pytest.raises(RuntimeError, match="test search failed"):
        request()
    assert slot.released == 1


@pytest.mark.parametrize("params", [{"time": "8:0"}, {"date": "2026-1-9"}])
def test_planner_requires_exact_date_and_time_format(params):
    assert request(**params)[0] == 400


@pytest.mark.parametrize("timings", [False, True])
def test_missing_time_names_required_format(timings):
    response = (views.plan_timings if timings else views.plan)(
        RequestFactory().get("/api/plan", {"from": "1", "to": "4", "date": "2026-10-19"})
    )
    assert response.status_code == 400
    assert "time must be HH:MM" in json.loads(response.content)["error"]
