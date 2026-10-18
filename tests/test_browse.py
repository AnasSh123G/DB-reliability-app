"""The line -> direction -> stop -> time browser.

Exercises the three queries behind it (line_directions, line_stations,
trips_around) with a line that has an uneven number of runs per direction, so
the "last 2 before/at, first 1 after" logic has to wrap around midnight and
cope with fewer than 3 total candidates.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    DAY_TYPE_SUNDAY_HOLIDAY,
    ServiceLine,
    Station,
    StationStop,
    Train,
)
from dbrel.ingest.runs import tt_feed_version
from dbrel.ingest.timeutil import service_date_anchor
from dbrel.web import queries

LINE_ID = 1
ROUTE = ["Aachen Hbf", "Krefeld Hbf", "Duisburg Hbf"]
UNCALLED_STATION_ID = 4  # Mönchengladbach Hbf: on no run's route at all.

# Three trains towards Duisburg (direction 0), calling at Krefeld Hbf at 06:00,
# 12:00 and 18:00, and one towards Aachen (direction 1).
RUNS = [
    (10, "RE 10", ROUTE, 6 * 3600),
    (11, "RE 11", ROUTE, 12 * 3600),
    (12, "RE 12", ROUTE, 18 * 3600),
    (20, "RE 20", ROUTE[::-1], 9 * 3600),
]


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        fv_id = tt_feed_version(s)
        s.add(ServiceLine(id=LINE_ID, identity_key="RE/1", category="RE", line="RE1",
                          display_name="RE1", search_name="re1", operator="DB"))
        for index, name in enumerate(ROUTE, start=1):
            s.add(Station(id=index, natural_key=f"s{index}", name=name,
                          lat=50.0 + index, lon=6.5, tracked=1))
        s.add(Station(id=UNCALLED_STATION_ID, natural_key="s4", name="Mönchengladbach Hbf",
                      lat=51.2, lon=6.4, tracked=1))
        s.flush()

        for train_id, name, path, krefeld_dep in RUNS:
            s.add(Train(
                id=train_id, source=SOURCE_TT, identity_key=f"tts:RE/{train_id}",
                category="RE", segment="RV", train_number=train_id, operator="DB",
                display_name=name, line_id=LINE_ID,
                origin_name=path[0], dest_name=path[-1],
                n_stops=len(path), route_path="|".join(path),
                first_departure_sec=krefeld_dep - 600,
            ))
        s.flush()

        # Every run is monitored along its whole three-stop route, one minute
        # apart per stop, timed so it calls at Krefeld Hbf (station 2) exactly
        # at krefeld_dep -- the "stations follow travel order" tests need all
        # three, the "around a time" tests only look at Krefeld Hbf.
        station_ids = {"Aachen Hbf": 1, "Krefeld Hbf": 2, "Duisburg Hbf": 3}
        for train_id, _name, path, krefeld_dep in RUNS:
            krefeld_pos = path.index("Krefeld Hbf")
            for seq, name in enumerate(path, start=1):
                offset = (seq - 1 - krefeld_pos) * 60
                s.add(StationStop(
                    feed_version_id=fv_id, train_id=train_id, stop_sequence=seq,
                    station_id=station_ids[name],
                    planned_arrival_sec=krefeld_dep + offset - 60,
                    planned_departure_sec=krefeld_dep + offset,
                ))
        s.flush()
        yield s


def test_stations_follow_travel_order_forward(session):
    stations = queries.line_stations(session, LINE_ID, 0)
    assert [s["name"] for s in stations] == ROUTE


def around(session, time_str, direction=0, station_id=2):
    hh, mm = time_str.split(":")
    return queries.trips_around(
        session, LINE_ID, direction, station_id, int(hh) * 3600 + int(mm) * 60
    )


def test_between_two_calls_picks_the_nearest_on_each_side(session):
    # 14:00 sits between the 12:00 and 18:00 Krefeld departures, with 06:00
    # further back -- the last two at/before are 06:00 and 12:00, not 12:00
    # twice.
    trips = around(session, "14:00")["trips"]
    assert [t["number_name"] for t in trips] == ["RE 10", "RE 11", "RE 12"]
    assert [t["relation"] for t in trips] == ["before", "before", "after"]


class TestResolveDayType:
    """The date field on the browse widget, resolved through the holiday table.

    Not the day-of-week alone: a date only counts as Sunday/holiday pooling if
    resolve_day_type says so, and it checks public_holiday -- the same table
    the rest of the app checks. Holidays are per-Bundesland, so a case that
    passes a station also has to pass that station's (resolved) state.
    """


    def test_a_sunday_resolves_to_sunday_holiday_with_no_table_rows_at_all(self, session):
        assert queries.resolve_day_type(session, "2026-09-20") == DAY_TYPE_SUNDAY_HOLIDAY


def _add_second_variant_of_re11_at_krefeld(session):
    """RE 11 seen on a second route variant: a second station_stop row for the
    very same Krefeld call, timed a minute later as variants often are."""
    original = session.query(StationStop).filter_by(train_id=11, station_id=2).one()
    session.add(StationStop(
        feed_version_id=original.feed_version_id, train_id=11, stop_sequence=7,
        station_id=2, planned_arrival_sec=original.planned_arrival_sec + 60,
        planned_departure_sec=original.planned_departure_sec + 60, path_sha1="other",
    ))
    session.flush()


def test_a_trip_on_two_route_variants_is_on_the_board_once(session):
    _add_second_variant_of_re11_at_krefeld(session)
    now = service_date_anchor(date.today()) + 12 * 3600
    calls = queries.station_board(session, 2, now=now)["calls"]
    assert [c["number_name"] for c in calls].count("RE 11") == 1


# --- runs asked for by position -----------------------------------------------------------------


def by_position(session, time_str, offset, count):
    hh, mm = time_str.split(":")
    return queries.trips_around(
        session, LINE_ID, 0, 2, int(hh) * 3600 + int(mm) * 60, offset=offset, count=count
    )["trips"]


