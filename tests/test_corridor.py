"""Line-less trains (ICE, IC, ...): grouped into a route around the chosen
train, with averages looked up per train instead of per line."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.direction import corridor_trains_by_direction
from dbrel.identity import SOURCE_TT
from dbrel.ingest.runs import tt_feed_version
from dbrel.models import Station, StationStop, Train, TrainStopDelayAverage
from dbrel.web import queries

ROUTE = ["Köln Hbf", "Hannover Hbf", "Berlin Hbf"]


def _train(id_, number, route, first_dep, category="ICE", line_id=None):
    return Train(
        id=id_, source=SOURCE_TT, identity_key=f"tts:{category}/{number}", category=category,
        train_number=number, display_name=f"{category} {number}", line_id=line_id,
        origin_name=route[0], dest_name=route[-1], route_path="|".join(route),
        first_departure_sec=first_dep,
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        fv_id = tt_feed_version(s)
        s.add(Station(id=2, natural_key="hannover@52.4,9.7", name="Hannover Hbf",
                      lat=52.4, lon=9.7, tracked=1))
        s.add_all([
            _train(10, 10, ROUTE, 8 * 3600),                       # the chosen train
            _train(12, 12, ROUTE, 12 * 3600),                      # same way, later
            _train(11, 11, ROUTE[::-1], 9 * 3600),                 # back the other way
            _train(14, 14, ["Hannover Hbf", "Berlin Hbf"], 10 * 3600),  # runs within
            _train(20, 20, ["München Hbf", "Nürnberg Hbf"], 7 * 3600),  # elsewhere
            _train(30, 30, ROUTE, 8 * 3600, category="IC"),        # other product
        ])
        s.flush()
        for train_id, dep in ((10, 10 * 3600), (12, 14 * 3600), (11, 11 * 3600), (14, 10 * 3600)):
            s.add(StationStop(feed_version_id=fv_id, train_id=train_id, stop_sequence=2,
                              station_id=2, planned_arrival_sec=dep - 120,
                              planned_departure_sec=dep))
        s.add(TrainStopDelayAverage(
            train_id=10, station_id=2, trip_start_time="08:00", sched_time="10:00",
            is_holiday=0, n_arr=5, mean_arr_delay_s=120.0, n_dep=5, mean_dep_delay_s=240.0,
            built_at=0,
        ))
        s.flush()
        yield s


def test_route_groups_same_stretch_trains_of_the_same_kind(session):
    _, halves = corridor_trains_by_direction(session, 10)
    assert sorted(t.id for t in halves[0]) == [10, 12, 14]
    assert [t.id for t in halves[1]] == [11]


def test_other_regions_and_other_products_are_left_out(session):
    _, halves = corridor_trains_by_direction(session, 10)
    members = {t.id for half in halves for t in half}
    assert 20 not in members and 30 not in members


def test_a_train_with_a_line_has_no_corridor(session):
    session.add(_train(40, 40, ROUTE, 8 * 3600, category="RE", line_id=None))
    session.flush()
    session.get(Train, 40).line_id = 999
    assert corridor_trains_by_direction(session, 40) is None


def test_directions_are_labelled_by_destination(session):
    labels = {d["direction"]: d["label"] for d in queries.corridor_directions(session, 10)["directions"]}
    assert labels == {0: "Berlin Hbf", 1: "Köln Hbf"}


def test_services_around_a_time_use_per_train_averages(session):
    result = queries.corridor_trips_around(session, 10, 0, 2, 11 * 3600)
    trip = next(t for t in result["trips"] if t["train_id"] == 10)
    assert trip["arr_pred"] == {"p50_min": 2, "lo_min": 2, "hi_min": 2}
    assert trip["dep_pred"] == {"p50_min": 4, "lo_min": 4, "hi_min": 4}
    other = next(t for t in result["trips"] if t["train_id"] == 12)
    assert other["dep_pred"] is None  # no rows for ICE 12 -- never fabricated


def test_train_route_page_also_uses_per_train_averages(session):
    stop = next(s for s in queries.train_route(session, 10)["stops"] if s["name"] == "Hannover Hbf")
    assert stop["dep_pred"] == {"p50_min": 4, "lo_min": 4, "hi_min": 4}


def test_another_days_route_variant_adds_no_stops(session):
    """ICE numbers run different routes on different days; only the variant
    the route was grouped by (train.path_sha1) may contribute stops."""
    fv_id = session.execute(select(StationStop.feed_version_id)).scalars().first()
    session.add(Station(id=9, natural_key="bremen@53.1,8.8", name="Bremen Hbf",
                        lat=53.1, lon=8.8, tracked=1))
    for train in session.execute(select(Train)).scalars():
        train.path_sha1 = "main"
    session.execute(update(StationStop).values(path_sha1="main"))
    session.add(StationStop(feed_version_id=fv_id, train_id=12, stop_sequence=5, station_id=9,
                            planned_arrival_sec=15 * 3600, planned_departure_sec=15 * 3600 + 60,
                            path_sha1="detour"))
    session.flush()
    names = [s["name"] for s in queries.corridor_stations(session, 10, 0)]
    assert "Bremen Hbf" not in names
    assert "Hannover Hbf" in names


def test_train_route_forecasts_the_given_run_day(session, monkeypatch):
    """A route opened from a Prognose run or a board call is forecast for that run's day."""
    from datetime import date

    seen = []

    def fake_model_input(_session, _model, **kwargs):
        seen.append(kwargs["service_date"])
        return None

    monkeypatch.setattr(queries, "current_model", lambda: ("stamp", object()))
    monkeypatch.setattr(queries, "load_holiday_dates", lambda _session: set())
    monkeypatch.setattr(queries, "_model_input", fake_model_input)
    queries.train_route(session, 10, service_date=date(2026, 10, 20))
    assert seen and set(seen) == {date(2026, 10, 20)}
