from datetime import date, datetime

import numpy as np
import pytest

from dbrel.config import TZ
from planner_fixture import make_timetable


def epoch(day, clock):
    return int(datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=TZ).timestamp())


def departures(tt, station, day, lo="07:00", hi="09:00"):
    return tt.departures(tt.si_of_id[station], epoch(day, lo), epoch(day, hi))


def test_faulty_step_cut_and_station_arrivals():
    tt = make_timetable()
    assert tt.var_cut[np.nonzero(tt.var_train == 500)[0][0]] == 2
    assert not tt.si_has_arrival[tt.si_of_id[9]]
    assert not tt.si_has_arrival[tt.si_of_id[1]]
    assert tt.si_has_arrival[tt.si_of_id[4]]
    assert tt.stop_si.size == 22
    assert tt.nbytes() == sum(
        v.nbytes for v in vars(tt).values() if isinstance(v, np.ndarray)
    )


@pytest.mark.parametrize(
    "day,trains",
    [
        ("2026-10-19", [102, 100, 300]),
        ("2026-10-18", [102, 100]),
        ("2026-10-10", [102, 100, 700]),
        ("2026-10-03", [102, 100]),
    ],
)
def test_departures_follow_profile_and_holidays(day, trains):
    tt = make_timetable()
    ds = departures(tt, 1, day)
    assert [tt.var_train[d.variant] for d in ds] == trains
    assert [d.dep_epoch for d in ds] == sorted(d.dep_epoch for d in ds)


def test_previous_service_date_after_midnight():
    tt = make_timetable()
    ds = departures(tt, 2, "2026-10-20", "00:10", "01:00")
    assert len(ds) == 1
    assert tt.var_train[ds[0].variant] == 600
    assert ds[0].service_date == date(2026, 10, 19)
    assert ds[0].dep_epoch == epoch("2026-10-20", "00:21")


def test_departure_boundaries_inclusive():
    tt = make_timetable()
    ds = departures(tt, 1, "2026-10-19", "08:00", "08:00")
    assert [tt.var_train[d.variant] for d in ds] == [100]


def test_unsorted_sparse_rows_and_faulty_backward_step():
    from dbrel.planner.constants import NO_TIME
    from dbrel.planner.timetable import PlannerTimetable

    tt = PlannerTimetable.from_columns(
        stations=[
            (1, "A", None, None, None),
            (2, "B", 51.0, 7.0, None),
            (3, "C", 51.0, 7.5, None),
        ],
        train_id=np.array([10, 10, 10, 10]),
        path_code=np.zeros(4, dtype=np.int32),
        path_values=[None],
        stop_sequence=np.array([40, 10, 30, 20]),
        station_id=np.array([3, 1, 2, 2]),
        arr=np.array([90, NO_TIME, NO_TIME, 110]),
        dep=np.array([NO_TIME, 100, NO_TIME, 120]),
        chosen={10: [None] * 7},
        line_key_of_train={10: "cX"},
        holiday_set=set(),
        first_date=None,
        last_date=None,
    )
    assert tt.stop_seq.tolist() == [10, 20, 30, 40]
    assert tt.var_cut.tolist() == [
        3
    ]  # the missing row does not reset the prior departure
    assert tt.dep_time.tolist() == [100, 120]
    assert not tt.si_has_arrival[2]


def _database():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    session = Session(create_engine("sqlite://"))
    statements = [
        "CREATE TABLE station(id INTEGER, name TEXT, lat REAL, lon REAL, state TEXT)",
        "CREATE TABLE train(id INTEGER, line_id INTEGER, category TEXT)",
        "CREATE TABLE station_stop(feed_version_id INTEGER, train_id INTEGER, path_sha1 TEXT, stop_sequence INTEGER, station_id INTEGER, planned_arrival_sec INTEGER, planned_departure_sec INTEGER)",
        "CREATE TABLE train_variant_weekday(train_id INTEGER, path_sha1 TEXT, weekday INTEGER, n_runs INTEGER)",
        "CREATE TABLE running_profile_window(first_date TEXT, last_date TEXT)",
        "CREATE TABLE public_holiday(date TEXT, state TEXT, partial INTEGER)",
        "INSERT INTO station VALUES (1,'A',51,7,'DE-NW'),(2,'B',51,8,'DE-NW')",
        "INSERT INTO train VALUES (10,5,'RE'),(11,NULL,'I-C E'),(12,NULL,NULL)",
        "INSERT INTO running_profile_window VALUES ('2026-09-22','2026-10-19')",
        "INSERT INTO train_variant_weekday VALUES (10,NULL,0,4)",
        "INSERT INTO station_stop VALUES (1,10,NULL,2,2,30600,NULL),(1,10,NULL,1,1,NULL,28800)",
        "INSERT INTO station_stop VALUES (1,10,'stale',1,1,NULL,28000)",
    ]
    for statement in statements:
        session.execute(text(statement))
    return session


def test_database_loader_filters_stale_variant_and_line_keys(monkeypatch):
    from sqlalchemy import text
    from dbrel.planner.timetable import load_timetable, _line_keys
    from dbrel.web import queries

    monkeypatch.setattr(queries, "tt_feed_version_id", lambda session: 1)
    with _database() as session:
        assert _line_keys(session) == {10: "sl5", 11: "cICE", 12: "cX"}
        session.execute(
            text(
                "CREATE TABLE passenger_line_member(train_id INTEGER, passenger_line_id INTEGER)"
            )
        )
        assert _line_keys(session)[10] == "sl5"
        session.execute(text("INSERT INTO passenger_line_member VALUES (10,99)"))
        tt = load_timetable(session)
    assert tt.var_line_key == ["pl99"]
    assert tt.var_path == [None]
    assert tt.stop_seq.tolist() == [1, 2]
    assert tt.stop_si.size == 2
    assert tt.first_date == date(2026, 9, 22)
    assert tt.last_date == date(2026, 10, 19)
    assert len(departures(tt, 1, "2026-10-19")) == 1


def test_missing_feed_returns_all_stations_empty_timetable(monkeypatch):
    from dbrel.planner.timetable import load_timetable
    from dbrel.web import queries

    monkeypatch.setattr(queries, "tt_feed_version_id", lambda session: None)
    with _database() as session:
        tt = load_timetable(session)
    assert tt.station_id.tolist() == [1, 2]
    assert tt.stop_si.size == tt.var_train.size == 0
    assert departures(tt, 1, "2026-10-19") == []
    assert not tt.si_has_arrival.any()


def test_get_timetable_builds_once_per_database(monkeypatch):
    from dbrel.planner import timetable
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    monkeypatch.setattr(timetable, "_cache", {})
    built = []

    def load(session):
        tt = object()
        built.append(tt)
        return tt

    monkeypatch.setattr(timetable, "load_timetable", load)
    with (
        Session(create_engine("sqlite://")) as first,
        Session(create_engine("sqlite:///other.sqlite")) as other,
    ):
        assert timetable.get_timetable(first) is timetable.get_timetable(first)
        assert timetable.get_timetable(other) is not timetable.get_timetable(first)
    assert len(built) == 2


def test_weekly_calls_count_each_stop_once_per_running_weekday():
    # Station 2 sees six daily trains; station 3's calls include train 500's, cut for its
    # faulty step but still a train calling there.
    assert make_timetable().weekly_calls() == {1: 34, 2: 42, 3: 28, 4: 34, 9: 0}


def test_travel_floor_is_the_least_scheduled_ride_time_whatever_the_day():
    from dbrel.planner.constants import UNREACHABLE_S

    tt = make_timetable()
    floor = tt.travel_floor(tt.si_of_id[4])
    seconds = {sid: int(floor[tt.si_of_id[sid]]) for sid in (1, 2, 3, 4, 9)}
    # D itself; C-D by train 400; B-D by the night train 600 (39 minutes, the quickest); A-D by
    # train 700, which runs only on Saturdays; Z has no trains at all.
    assert seconds == {4: 0, 3: 1800, 2: 2340, 1: 3300, 9: UNREACHABLE_S}
