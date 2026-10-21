"""Search narrowed to one Bundesland: lines and trains that stop there, stations from everywhere.

Two RE4 lines share the name, one in North Rhine-Westphalia (Aachen - Dortmund) and one in
Mecklenburg-Vorpommern (Schwerin - Pasewalk); a third line, RE9, crosses from NW into Lower Saxony.
"""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.derive.passenger_lines import build_passenger_lines
from dbrel.identity import SOURCE_TT
from dbrel.ingest.runs import tt_feed_version
from dbrel.models import PassengerLineMember, SearchAlias, ServiceLine, Station, StationStop, Train
from dbrel.web import queries

# id: (name, state)
STATIONS = {1: ("Aachen Hbf", "DE-NW"), 2: ("Dortmund Hbf", "DE-NW"), 3: ("Schwerin Hbf", "DE-MV"),
            4: ("Pasewalk", "DE-MV"), 5: ("Osnabrück Hbf", "DE-NI")}


def _train(s, fv_id, train_id, line_id, station_ids, *, category="RE", segment="RV"):
    """A running train calling at `station_ids` in order, with its schedule rows."""
    names = [STATIONS[i][0] for i in station_ids]
    s.add(Train(id=train_id, source=SOURCE_TT, identity_key=f"tts:{category}/{train_id}", category=category,
                segment=segment, train_number=train_id, operator="X", display_name=f"{category} {train_id}",
                line_id=line_id, origin_name=names[0], dest_name=names[-1], n_stops=len(names),
                route_path="|".join(names), path_sha1=f"p{train_id}", last_seen_date="2026-09-28"))
    s.flush()
    for seq, station_id in enumerate(station_ids, start=1):
        s.add(StationStop(feed_version_id=fv_id, train_id=train_id, stop_sequence=seq, station_id=station_id,
                          path_sha1=f"p{train_id}", planned_arrival_sec=8 * 3600 + seq * 600,
                          planned_departure_sec=8 * 3600 + seq * 600 + 60))


@pytest.fixture
def session():
    """Stations in three states, two RE4 lines, a cross-border RE9 and a line-less ICE."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        fv_id = tt_feed_version(s)
        for station_id, (name, state) in STATIONS.items():
            s.add(Station(id=station_id, natural_key=f"{name}@{station_id}", name=name, lat=50.0, lon=7.0,
                          state=state, tracked=1))
        s.add_all([
            ServiceLine(id=1, identity_key="RE/4", category="RE", line="4", display_name="RE 4", search_name="re4"),
            ServiceLine(id=2, identity_key="RE/9", category="RE", line="9", display_name="RE 9", search_name="re9"),
        ])
        s.flush()
        _train(s, fv_id, 100, 1, [1, 2])
        _train(s, fv_id, 101, 1, [2, 1])
        _train(s, fv_id, 200, 1, [3, 4])
        _train(s, fv_id, 300, 2, [2, 5])
        _train(s, fv_id, 618, None, [1, 2, 5], category="ICE", segment="FV")
        s.add_all([
            SearchAlias(alias_norm="re4", alias_display="RE 4", kind="CAT_LINE", weight=90, line_id=1),
            SearchAlias(alias_norm="re9", alias_display="RE 9", kind="CAT_LINE", weight=90, line_id=2),
            SearchAlias(alias_norm="ice618", alias_display="ICE 618", kind="CAT_NUMBER", weight=100, train_id=618),
            SearchAlias(alias_norm="pasewalk", alias_display="Pasewalk", kind="STATION_NAME", weight=100,
                        station_id=4),
        ])
        s.flush()
        yield s


def _lines(session, term, state):
    """The endpoints of each line result for `term` searched in `state`."""
    return sorted(r["stops"] for r in queries.suggest(session, term, state=state) if r["kind"] == "line")


@pytest.fixture
def passenger_session(session):
    """The same data with passenger lines built, as on the site."""
    build_passenger_lines(session)
    session.flush()
    assert session.get(PassengerLineMember, 100).passenger_line_id != session.get(
        PassengerLineMember, 200).passenger_line_id
    return session


def test_without_a_state_every_line_of_the_name_is_listed(passenger_session):
    assert _lines(passenger_session, "re4", None) == ["Aachen Hbf – Dortmund Hbf", "Schwerin Hbf – Pasewalk"]


def test_a_state_keeps_only_the_line_that_stops_there(passenger_session):
    assert _lines(passenger_session, "re4", "DE-NW") == ["Aachen Hbf – Dortmund Hbf"]
    assert _lines(passenger_session, "re4", "DE-MV") == ["Schwerin Hbf – Pasewalk"]
    assert _lines(passenger_session, "re4", "DE-BY") == []


def test_a_line_crossing_a_border_is_found_from_both_states(passenger_session):
    assert _lines(passenger_session, "re9", "DE-NW") == _lines(passenger_session, "re9", "DE-NI") == [
        "Dortmund Hbf – Osnabrück Hbf"]


def _suggest_response(query: str):
    """The /api/suggest view's response for `query` (a query string)."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dbrel.web.settings")
    import django

    django.setup()
    from django.test import RequestFactory

    from dbrel.web import views

    return views.suggest(RequestFactory().get(f"/api/suggest?{query}"))


def test_the_api_refuses_an_unknown_state():
    response = _suggest_response("q=re4&state=Bavaria")
    assert response.status_code == 400
    assert "DE-BY" in json.loads(response.content)["error"]
