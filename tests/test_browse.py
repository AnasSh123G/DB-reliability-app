"""The line -> direction -> stop -> time browser.

Exercises the three queries behind it (line_directions, line_stations,
trips_around) with a line that has an uneven number of runs per direction, so
the "last 2 before/at, first 1 after" logic has to wrap around midnight and
cope with fewer than 3 total candidates.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    DAY_TYPE_SUNDAY_HOLIDAY,
    DAY_TYPE_WEEKDAY,
    PublicHoliday,
    ServiceLine,
    Station,
    StationStop,
    Train,
    TrainStopAverage,
)
from dbrel.timetables.runs import tt_feed_version
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


def test_directions_are_labelled_by_where_they_end_up(session):
    result = queries.line_directions(session, LINE_ID)
    labels = {d["label"]: d["n_runs"] for d in result["directions"]}
    assert labels == {"towards Duisburg Hbf": 3, "towards Aachen Hbf": 1}


def test_unknown_line_directions_is_none(session):
    assert queries.line_directions(session, 999) is None


def test_stations_follow_travel_order_forward(session):
    stations = queries.line_stations(session, LINE_ID, 0)
    assert [s["name"] for s in stations] == ROUTE


def test_ordering_survives_a_station_name_that_disagrees_with_route_path(session):
    """IRIS's route_path text and our own station names do not always agree
    (route_path might say "Krefeld-Hbf", the station row says "Krefeld Hbf") --
    ordering by stop_sequence rather than by matching that text must not care.
    """
    session.get(Station, 2).name = "Krefeld Hbf, renamed since the timetable ran"
    stations = queries.line_stations(session, LINE_ID, 0)
    assert [s["id"] for s in stations] == [1, 2, 3]


def test_stations_follow_travel_order_reversed(session):
    stations = queries.line_stations(session, LINE_ID, 1)
    assert [s["name"] for s in stations] == list(reversed(ROUTE))


def test_invalid_direction_is_none(session):
    assert queries.line_stations(session, LINE_ID, 2) is None


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
    assert [t["name"] for t in trips] == ["RE 10", "RE 11", "RE 12"]
    assert [t["relation"] for t in trips] == ["before", "before", "after"]


def test_exactly_on_a_scheduled_time_counts_as_before(session):
    trips = around(session, "12:00")["trips"]
    assert [t["name"] for t in trips] == ["RE 10", "RE 11", "RE 12"]
    assert trips[1]["relation"] == "before"


def test_before_the_first_call_wraps_to_the_end_of_the_day(session):
    # 02:00 has no calls before it today -- the "last two" wrap to the previous
    # day's last two, 12:00 and 18:00, and "next" wraps back to 06:00. They
    # keep their slot's label (before/before/after) even though every one of
    # them falls later in the clock than 02:00.
    trips = around(session, "02:00")["trips"]
    assert [t["name"] for t in trips] == ["RE 11", "RE 12", "RE 10"]
    assert [t["relation"] for t in trips] == ["before", "before", "after"]


def test_after_the_last_call_wraps_to_tomorrows_first(session):
    trips = around(session, "23:00")["trips"]
    assert [t["name"] for t in trips] == ["RE 11", "RE 12", "RE 10"]
    assert trips[-1]["relation"] == "after"


def test_the_lone_run_the_other_way_is_never_mixed_in(session):
    trips = around(session, "14:00")["trips"]
    assert "RE 20" not in {t["name"] for t in trips}
    assert around(session, "10:00", direction=1)["trips"][0]["name"] == "RE 20"


def test_fewer_than_three_runs_does_not_fabricate_a_third(session):
    """Direction 1 has exactly one run -- it must appear once, not three times."""
    trips = around(session, "10:00", direction=1)["trips"]
    assert [t["name"] for t in trips] == ["RE 20"]


def test_scheduled_times_are_the_ones_at_the_chosen_station(session):
    trips = around(session, "14:00")["trips"]
    re11 = next(t for t in trips if t["name"] == "RE 11")
    assert re11["sched_arr"] == "11:59"
    assert re11["sched_dep"] == "12:00"


def test_average_attaches_when_present_and_is_none_when_not(session):
    session.add(TrainStopAverage(
        day_type=DAY_TYPE_WEEKDAY, train_id=11, station_id=2, sched_sec=12 * 3600,
        sched_kind="DEP", sched_dep_sec=12 * 3600, line_id=LINE_ID,
        n_dep=8, n_arr=8, n_runs=8, n_known=8,
        avg_dep_delay_s=90.0, pct_dep_punctual=0.75,
        computed_at=1, rule_version=1,
    ))
    session.flush()
    trips = around(session, "14:00")["trips"]
    by_name = {t["name"]: t for t in trips}
    assert by_name["RE 11"]["dep_avg"]["avg_min"] == 1.5
    assert by_name["RE 10"]["dep_avg"] is None


def test_unknown_line_or_station_is_none(session):
    assert queries.trips_around(session, 999, 0, 2, 0) is None
    assert queries.trips_around(session, LINE_ID, 0, 999, 0) is None


def test_a_stop_with_no_calls_in_that_direction_is_empty_not_none(session):
    result = queries.trips_around(session, LINE_ID, 0, UNCALLED_STATION_ID, 12 * 3600)
    assert result is not None
    assert result["trips"] == []


class TestResolveDayType:
    """The date field on the browse widget, resolved through the holiday table.

    Not the day-of-week alone: a date only counts as Sunday/holiday pooling if
    resolve_day_type says so, and it checks public_holiday -- the same table
    day_type_of checks everywhere else in the app.
    """

    def test_an_ordinary_weekday_resolves_to_weekday(self, session):
        assert queries.resolve_day_type(session, "2026-09-15") == DAY_TYPE_WEEKDAY

    def test_a_sunday_resolves_to_sunday_holiday_with_no_table_rows_at_all(self, session):
        assert queries.resolve_day_type(session, "2026-09-20") == DAY_TYPE_SUNDAY_HOLIDAY

    def test_a_table_row_turns_a_plain_weekday_into_a_holiday(self, session):
        session.add(PublicHoliday(date="2026-09-15", name="Betriebsruhetag (test)"))
        session.flush()
        assert queries.resolve_day_type(session, "2026-09-15") == DAY_TYPE_SUNDAY_HOLIDAY

    def test_unusable_date_resolves_to_none(self, session):
        assert queries.resolve_day_type(session, "not-a-date") is None


def test_trips_around_switches_pools_with_the_resolved_day_type(session):
    """What the view does: resolve_day_type's result feeds straight into trips_around."""
    session.add(PublicHoliday(date="2026-09-15", name="Betriebsruhetag (test)"))
    session.add(TrainStopAverage(
        day_type=DAY_TYPE_WEEKDAY, train_id=11, station_id=2, sched_sec=12 * 3600,
        sched_kind="DEP", sched_dep_sec=12 * 3600, n_dep=4, n_arr=4, n_runs=4,
        n_known=4, avg_dep_delay_s=60.0, pct_dep_punctual=0.9,
        computed_at=1, rule_version=1,
    ))
    session.add(TrainStopAverage(
        day_type=DAY_TYPE_SUNDAY_HOLIDAY, train_id=11, station_id=2, sched_sec=12 * 3600,
        sched_kind="DEP", sched_dep_sec=12 * 3600, n_dep=9, n_arr=9, n_runs=9,
        n_known=9, avg_dep_delay_s=900.0, pct_dep_punctual=0.1,
        computed_at=1, rule_version=1,
    ))
    session.flush()

    for chosen_date, expected_n in (("2026-09-16", 4), ("2026-09-15", 9)):
        day_type = queries.resolve_day_type(session, chosen_date)
        trips = queries.trips_around(
            session, LINE_ID, 0, 2, 14 * 3600, day_type=day_type
        )["trips"]
        assert next(t for t in trips if t["name"] == "RE 11")["dep_avg"]["n"] == expected_n
