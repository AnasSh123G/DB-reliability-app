"""The station board.

No file covered this query at all before -- which is how a real bug shipped
unnoticed: a terminating call (arrival only, no departure) was falling back to
its arrival time to ask the model for a *departure* delay, producing a number
for an event that does not exist. The regression test here is the one that
would have caught it.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.gtfs.timeutil import service_date_anchor
from dbrel.identity import SOURCE_TT
from dbrel.models import Station, StationStop, Train
from dbrel.timetables.runs import tt_feed_version
from dbrel.web import queries

STATION_ID = 1
THROUGH_TRAIN_ID = 10
TERMINUS_TRAIN_ID = 20

# A real instant to anchor the board's window against, rather than treating a
# seconds-of-service-day value as if it were an epoch.
ANCHOR = service_date_anchor(date(2026, 9, 21))
NOW = ANCHOR + 8 * 3600


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        fv_id = tt_feed_version(s)
        s.add(Station(id=STATION_ID, natural_key="bochum@51.48,7.22",
                      name="Bochum Hbf", lat=51.48, lon=7.22, tracked=1))
        s.add(Train(id=THROUGH_TRAIN_ID, source=SOURCE_TT, identity_key="tts:RE/10",
                    category="RE", segment="RV", train_number=10, operator="DB",
                    display_name="RE 10", n_stops=3, first_departure_sec=7 * 3600))
        s.add(Train(id=TERMINUS_TRAIN_ID, source=SOURCE_TT, identity_key="tts:RE/20",
                    category="RE", segment="RV", train_number=20, operator="DB",
                    display_name="RE 20", n_stops=3, first_departure_sec=6 * 3600))
        s.flush()
        # A through call: both an arrival and a departure.
        s.add(StationStop(feed_version_id=fv_id, train_id=THROUGH_TRAIN_ID, stop_sequence=2,
                          station_id=STATION_ID,
                          planned_arrival_sec=8 * 3600, planned_departure_sec=8 * 3600 + 120))
        # A terminating call: arrival only, no departure at all.
        s.add(StationStop(feed_version_id=fv_id, train_id=TERMINUS_TRAIN_ID, stop_sequence=3,
                          station_id=STATION_ID,
                          planned_arrival_sec=8 * 3600 + 300, planned_departure_sec=None))
        s.flush()
        yield s


def board_calls(session, **kwargs):
    return queries.station_board(session, STATION_ID, now=NOW, **kwargs)["calls"]


def test_a_terminating_call_gets_no_forecast_even_with_a_model(session, stub_ml_model):
    """The regression case: no departure exists, so nothing to predict."""
    calls = {c["train_id"]: c for c in board_calls(session)}
    assert calls[TERMINUS_TRAIN_ID]["terminates"] is True
    assert calls[TERMINUS_TRAIN_ID]["dep"] is None
    assert calls[TERMINUS_TRAIN_ID]["dep_pred"] is None


def test_a_through_call_does_get_a_forecast(session, stub_ml_model):
    calls = {c["train_id"]: c for c in board_calls(session)}
    assert calls[THROUGH_TRAIN_ID]["dep_pred"]["p50_min"] == 2.0


def test_no_forecast_anywhere_without_an_activated_model(session):
    calls = board_calls(session)
    assert calls and all(c["dep_pred"] is None for c in calls)


def test_unknown_station_is_none(session):
    assert queries.station_board(session, 999999) is None
