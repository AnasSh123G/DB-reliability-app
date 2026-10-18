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


