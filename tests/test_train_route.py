"""The train route view.

Only stops with scheduled times are shown. station_stop.stop_sequence is the
1-based index into the ppth of its own route variant, so route_path names are
only trusted for train.path_sha1's variant -- another variant's numbering would
put times on the wrong stations.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    DAY_TYPE_SUNDAY_HOLIDAY,
    SearchAlias,
    ServiceLine,
    Station,
    StationStop,
    Train,
    Trip,
)
from dbrel.ingest.runs import tt_feed_version
from dbrel.web import queries

ROUTE = ["Aachen Hbf", "Köln Hbf", "Duisburg Hbf", "Bochum Hbf", "Hamm(Westf)Hbf"]
TRAIN_ID = 500

# The scheduled departure at Bochum.
BOCHUM_DEP_SEC = 10 * 3600 + 60


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
                          planned_departure_sec=BOCHUM_DEP_SEC, platform_code="3"))
        s.flush()
        yield s


def test_only_stops_with_times_are_returned(session):
    route = queries.train_route(session, TRAIN_ID)
    assert [s["name"] for s in route["stops"]] == ["Duisburg Hbf", "Bochum Hbf"]
    assert route["n_monitored"] == 2
    assert route["train"]["n_stops"] == 2


def test_times_land_on_the_right_stop(session):
    """stop_sequence is the 1-based index into route_path."""
    stops = {s["name"]: s for s in queries.train_route(session, TRAIN_ID)["stops"]}
    assert stops["Duisburg Hbf"]["monitored"] is True
    assert stops["Duisburg Hbf"]["dep"] == "09:38"
    assert stops["Duisburg Hbf"]["platform"] == "12"
    assert stops["Bochum Hbf"]["arr"] == "10:00"


def test_another_variants_numbering_is_not_read_against_route_path(session):
    """The latest run's variant numbers its stops by its OWN ppth: seq 1 here is
    Bochum, not route_path[0] (Aachen). The name must follow the station row."""
    fv_id = tt_feed_version(session)
    session.get(Train, TRAIN_ID).path_sha1 = "full"
    session.query(StationStop).update({"path_sha1": "full"})
    session.add(StationStop(feed_version_id=fv_id, train_id=TRAIN_ID, stop_sequence=1,
                            station_id=4, planned_arrival_sec=10 * 3600,
                            planned_departure_sec=BOCHUM_DEP_SEC, path_sha1="short"))
    session.add(Trip(train_id=TRAIN_ID, service_date="2026-10-01", source_run_key="run-1",
                     path_sha1="short"))
    session.flush()
    stops = queries.train_route(session, TRAIN_ID)["stops"]
    assert [(s["name"], s["arr"]) for s in stops] == [("Bochum Hbf", "10:00")]


def test_the_route_shown_calls_at_the_stop_the_caller_came_from(session):
    """A train that last ran a short variant still shows the full one when opened from a stop only
    the full variant calls at (a board row for Bochum), as the board itself picked it."""
    fv_id = tt_feed_version(session)
    session.get(Train, TRAIN_ID).path_sha1 = "full"
    session.query(StationStop).update({"path_sha1": "full"})
    session.add(StationStop(feed_version_id=fv_id, train_id=TRAIN_ID, stop_sequence=1,
                            station_id=3, planned_arrival_sec=9 * 3600 + 2160,
                            planned_departure_sec=9 * 3600 + 2280, path_sha1="short"))
    session.add(Trip(train_id=TRAIN_ID, service_date="2026-10-02", source_run_key="run-short",
                     path_sha1="short"))
    session.add(Trip(train_id=TRAIN_ID, service_date="2026-10-01", source_run_key="run-full",
                     path_sha1="full"))
    session.flush()
    names = lambda route: [s["name"] for s in route["stops"]]  # noqa: E731
    assert names(queries.train_route(session, TRAIN_ID)) == ["Duisburg Hbf"]  # the latest run's variant
    assert names(queries.train_route(session, TRAIN_ID, station_id=4)) == ["Duisburg Hbf", "Bochum Hbf"]
    # Both variants call at Duisburg: the board's rule (most recent run) applies, i.e. the short one.
    assert names(queries.train_route(session, TRAIN_ID, station_id=3)) == ["Duisburg Hbf"]


def test_route_names_are_the_fallback_when_nothing_has_times(session):
    session.query(StationStop).delete()
    session.flush()
    route = queries.train_route(session, TRAIN_ID)
    assert [s["name"] for s in route["stops"]] == ROUTE
    assert route["n_monitored"] == 0


def test_no_forecast_without_average_data(session):
    """This fixture never populates stop_delay_average, so no figure is shown."""
    stops = {s["name"]: s for s in queries.train_route(session, TRAIN_ID)["stops"]}
    assert stops["Bochum Hbf"]["dep_pred"] is None
    assert stops["Duisburg Hbf"]["dep_pred"] is None


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
    runs = [run for d in detail["directions"] for run in d["runs"]]
    assert len(runs) == 1
    assert runs[0]["name"] == "NX 26811"
    assert runs[0]["n_monitored"] == 2
    assert runs[0]["url"] == f"/train/{TRAIN_ID}/"
    assert runs[0]["pred"] is None  # no stop_delay_average rows in this fixture


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


def test_regional_train_is_named_by_its_line_with_the_number_kept(session):
    train = queries.train_route(session, TRAIN_ID)["train"]
    assert (train["name"], train["number_name"]) == ("RE1", "NX 26811")


def test_long_distance_train_keeps_its_number(session):
    session.get(Train, TRAIN_ID).segment = "FV"
    session.flush()
    assert queries.train_route(session, TRAIN_ID)["train"]["name"] == "NX 26811"


def test_bare_category_line_is_not_used_as_a_name(session):
    session.get(ServiceLine, 9).line = "RE"
    session.get(ServiceLine, 9).display_name = "RE"
    session.flush()
    assert queries.train_route(session, TRAIN_ID)["train"]["name"] == "NX 26811"


def test_search_lists_a_line_once_rather_than_each_of_its_runs(session):
    session.add_all([
        SearchAlias(alias_norm="re1", alias_display="RE1", kind="CAT_LINE", weight=90, line_id=9),
        SearchAlias(alias_norm="nx26811", alias_display="NX 26811", kind="CAT_NUMBER",
                    weight=100, train_id=TRAIN_ID),
    ])
    session.flush()
    assert [r["kind"] for r in queries.suggest(session, "nx")] == []
    assert [r["kind"] for r in queries.suggest(session, "re")] == ["line"]
    by_number = queries.suggest(session, "nx 268")
    assert [(r["name"], r["number_name"]) for r in by_number] == [("RE1", "NX 26811")]
