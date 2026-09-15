"""The train route view.

The load-bearing assumption is that station_stop.stop_sequence is the 1-based
index into train.route_path, which is what lets a journey with three monitored
stops still render all twenty-five. Measured against the live database it holds
for every row; here it is pinned so a change to either side fails loudly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    BASIS_SYNTH,
    SearchAlias,
    ServiceLine,
    Station,
    StationStop,
    Train,
    TrainStationDelay,
)
from dbrel.timetables.runs import tt_feed_version
from dbrel.web import queries

ROUTE = ["Aachen Hbf", "Köln Hbf", "Duisburg Hbf", "Bochum Hbf", "Hamm(Westf)Hbf"]
TRAIN_ID = 500


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        fv_id = tt_feed_version(s)
        s.add(ServiceLine(id=9, identity_key="NX/RE1", category="NX", line="RE1",
                          display_name="RE1", search_name="re1", operator="NXRE"))
        # Only stops 3 and 4 of the five-stop route are monitored.
        s.add(Station(id=3, natural_key="duisburg@51.4,6.8", name="Duisburg Hbf",
                      lat=51.4, lon=6.8, tracked=1, eva=8000086))
        s.add(Station(id=4, natural_key="bochum@51.5,7.2", name="Bochum Hbf",
                      lat=51.5, lon=7.2, tracked=1, eva=8000041))
        s.add(Train(id=TRAIN_ID, source=SOURCE_TT, identity_key="tts:NX/26811",
                    category="NX", segment="RV", train_number=26811, operator="NXRE",
                    display_name="NX 26811", line_id=9,
                    origin_name="Aachen Hbf", dest_name="Hamm(Westf)Hbf",
                    n_stops=5, route_path="|".join(ROUTE)))
        s.flush()
        s.add(StationStop(feed_version_id=fv_id, train_id=TRAIN_ID, stop_sequence=3,
                          station_id=3, planned_arrival_sec=9 * 3600 + 2160,
                          planned_departure_sec=9 * 3600 + 2280, platform_code="12"))
        s.add(StationStop(feed_version_id=fv_id, train_id=TRAIN_ID, stop_sequence=4,
                          station_id=4, planned_arrival_sec=10 * 3600,
                          planned_departure_sec=10 * 3600 + 60, platform_code="3"))
        s.add(TrainStationDelay(station_id=4, train_id=TRAIN_ID, basis=BASIS_SYNTH,
                                n_arr=20, n_dep=20, n_runs=20, n_known=20,
                                avg_dep_delay_s=180.0, p50_dep_delay_s=120,
                                p90_dep_delay_s=480, pct_dep_punctual=0.85,
                                computed_at=1, rule_version=1))
        s.flush()
        yield s


def test_whole_route_is_returned_not_just_monitored_stops(session):
    route = queries.train_route(session, TRAIN_ID)
    assert [s["name"] for s in route["stops"]] == ROUTE
    assert route["n_monitored"] == 2


def test_times_land_on_the_right_stop(session):
    """stop_sequence is the 1-based index into route_path."""
    stops = {s["name"]: s for s in queries.train_route(session, TRAIN_ID)["stops"]}
    assert stops["Duisburg Hbf"]["monitored"] is True
    assert stops["Duisburg Hbf"]["dep"] == "09:38"
    assert stops["Duisburg Hbf"]["platform"] == "12"
    assert stops["Bochum Hbf"]["arr"] == "10:00"
    assert stops["Aachen Hbf"]["monitored"] is False
    assert stops["Aachen Hbf"]["dep"] is None


def test_average_attaches_only_where_there_is_one(session):
    stops = {s["name"]: s for s in queries.train_route(session, TRAIN_ID)["stops"]}
    assert stops["Bochum Hbf"]["dep_avg"]["avg_min"] == 3.0
    assert stops["Bochum Hbf"]["dep_avg"]["punctual_pct"] == 85
    # Monitored but no averages row computed yet.
    assert stops["Duisburg Hbf"]["dep_avg"] is None
    assert stops["Hamm(Westf)Hbf"]["dep_avg"] is None


def test_headline_average_covers_only_measured_stops(session):
    assert queries.train_route(session, TRAIN_ID)["avg_delay_min"] == 3.0


def test_unknown_train_returns_none(session):
    assert queries.train_route(session, 999999) is None


def test_past_midnight_times_keep_the_24_plus_hour_form(session):
    """A 01:10 stop on a run that left yesterday must read 25:10, not 01:10."""
    stop = session.query(StationStop).filter_by(stop_sequence=4).one()
    stop.planned_departure_sec = 25 * 3600 + 600
    session.flush()
    stops = {s["name"]: s for s in queries.train_route(session, TRAIN_ID)["stops"]}
    assert stops["Bochum Hbf"]["dep"] == "25:10"


def test_line_detail_lists_its_runs(session):
    detail = queries.line_detail(session, 9)
    assert detail["line"]["name"] == "RE1"
    assert detail["n_runs"] == 1
    run = detail["runs"][0]
    assert run["name"] == "NX 26811"
    assert run["n_monitored"] == 2
    assert run["url"] == f"/train/{TRAIN_ID}/"


def test_replacement_bus_is_flagged(session):
    session.add(ServiceLine(id=10, identity_key="Bus/RE1", category="Bus", line="RE1",
                            display_name="RE1", search_name="re1", operator="NX"))
    session.flush()
    assert queries.line_detail(session, 10)["line"]["replacement_bus"] is True
    assert queries.line_detail(session, 9)["line"]["replacement_bus"] is False


def test_suggest_returns_lines_and_trains_not_only_stations(session):
    session.add_all([
        SearchAlias(alias_norm="re1", alias_display="RE1", kind="CAT_LINE", weight=90, line_id=9),
        SearchAlias(alias_norm="nx26811", alias_display="NX 26811", kind="CAT_NUMBER",
                    weight=100, train_id=TRAIN_ID),
        SearchAlias(alias_norm="bochum", alias_display="Bochum", kind="STATION_SHORT",
                    weight=95, station_id=4),
    ])
    session.flush()

    kinds = {r["kind"] for r in queries.suggest(session, "re1")}
    assert kinds == {"line"}

    train_hit = queries.suggest(session, "NX 26811")
    assert train_hit[0]["kind"] == "train"
    assert train_hit[0]["url"] == f"/train/{TRAIN_ID}/"

    station_hit = queries.suggest(session, "bochum")
    assert station_hit[0]["kind"] == "station"
    assert station_hit[0]["url"] == "/station/4/"
