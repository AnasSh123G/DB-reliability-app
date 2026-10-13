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
from dbrel.ingest.timeutil import service_date_anchor
from dbrel.identity import SOURCE_TT
from dbrel.models import Station, StationStop, Train
from dbrel.ingest.runs import tt_feed_version
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


def test_no_forecast_anywhere_without_average_data(session):
    calls = board_calls(session)
    assert calls and all(c["dep_pred"] is None and c["arr_pred"] is None for c in calls)


def test_unknown_station_is_none(session):
    assert queries.station_board(session, 999999) is None


# --- a board for a chosen date and time -------------------------------------------------------


def _moment(query: str):
    """What the board view makes of `query`: (epoch or None, error or None)."""
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dbrel.web.settings")
    import django

    django.setup()
    from django.test import RequestFactory

    from dbrel.web import views

    return views._chosen_moment(RequestFactory().get(f"/api/station/1/board?{query}"))


def test_a_chosen_moment_centres_the_board_on_it_not_on_now(session):
    epoch, problem = _moment("date=2026-09-21&time=08:00")
    assert problem is None
    board = queries.station_board(session, STATION_ID, now=epoch)
    assert board["window"]["date"] == "2026-09-21"
    assert {c["train"] for c in board["calls"]} == {"RE 10", "RE 20"}
    far = queries.station_board(session, STATION_ID, now=epoch + 5 * 3600)
    assert far["calls"] == []


def test_no_date_and_time_means_now():
    assert _moment("") == (None, None)


def test_date_without_time_is_refused():
    epoch, problem = _moment("date=2026-09-21")
    assert epoch is None and "together" in problem


def test_an_unreadable_value_is_named():
    assert "'21.09.2026'" in _moment("date=21.09.2026&time=08:00")[1]
    assert "'8am'" in _moment("date=2026-09-21&time=8am")[1]


def test_the_view_answers_400_for_a_bad_moment():
    import json

    from django.test import RequestFactory

    from dbrel.web import views

    _moment("")  # sets Django up
    response = views.station_board(RequestFactory().get("/api/station/1/board?date=nope&time=08:00"), 1)
    assert response.status_code == 400
    assert "date" in json.loads(response.content)["error"]


def test_the_repeated_autumn_hour_means_the_first_one():
    first, _ = _moment("date=2026-10-25&time=02:30")
    after, _ = _moment("date=2026-10-25&time=03:30")
    assert after - first == 2 * 3600  # 02:30 CEST, then 02:30 CET, then 03:30 CET
