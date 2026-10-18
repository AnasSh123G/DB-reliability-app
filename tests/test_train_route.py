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
    ServiceLine,
    Station,
    StationStop,
    Train,
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


def test_past_midnight_times_keep_the_24_plus_hour_form(session):
    """A 01:10 stop on a run that left yesterday must read 25:10, not 01:10."""
    stop = session.query(StationStop).filter_by(stop_sequence=4).one()
    stop.planned_departure_sec = 25 * 3600 + 600
    session.flush()
    stops = {s["name"]: s for s in queries.train_route(session, TRAIN_ID)["stops"]}
    assert stops["Bochum Hbf"]["dep"] == "25:10"


def _retire_the_fixture_train(session, last_seen="2026-08-12", newest="2026-09-28"):
    """Make TRAIN_ID retired: last seen `last_seen`, while a running NX 26405 was seen `newest`."""
    session.get(Train, TRAIN_ID).last_seen_date = last_seen
    session.add(Train(id=TRAIN_ID + 1, source=SOURCE_TT, identity_key="tts:NX/26405", category="NX",
                      segment="RV", train_number=26405, operator="NXRE", display_name="NX 26405", line_id=9,
                      last_seen_date=newest))
    session.flush()


def test_a_retired_train_page_says_so_and_lists_no_stops(session):
    """A train unseen for over 14 days before the newest data gets the retired message, no stops."""
    _retire_the_fixture_train(session)
    route = queries.train_route(session, TRAIN_ID)
    assert route["retired"] == {"last_seen_date": "2026-08-12"}
    assert route["stops"] == [] and route["n_monitored"] == 0
    assert route["train"]["id"] == TRAIN_ID and route["train"]["n_stops"] == 0
    assert "retired" not in queries.train_route(session, TRAIN_ID + 1)


