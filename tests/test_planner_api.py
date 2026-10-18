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
        "stats",
    }
    assert len(body["journeys"]) == 3
    first = body["journeys"][0]
    assert first["legs"][0]["departure"] == {
        "epoch": int(datetime(2026, 10, 19, 8, tzinfo=TZ).timestamp()),
        "date": "2026-10-19",
        "time": "08:00",
    }
    assert first["n_changes"] == 1
    assert first["changes"][0]["planned_min"] == first["changes"][0]["left_min"] == 6


def test_busy_after_validation(monkeypatch):
    class Busy:
        def acquire(self, **kwargs):
            return False

        def release(self):
            raise AssertionError("unacquired slot released")

    monkeypatch.setattr(views, "_PLAN_SLOTS", Busy())
    assert request(to="12345")[0] == 404
    assert request() == (503, {"error": "the planner is busy, try again"})


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


