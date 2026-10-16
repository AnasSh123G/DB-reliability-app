"""Running-day profiles and state-specific variant selection."""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.models import PublicHoliday, Station, Train, Trip
from dbrel.planner.profile import (
    NOTHING,
    PROFILE_SQL,
    RunProfile,
    chosen_variants,
    day_key,
    load_run_profile,
    profile_params,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Station(id=1, natural_key="a", name="A", lat=51, lon=7))
        session.add(Train(id=1, identity_key="train1", last_seen_date="2026-09-27"))
        session.flush()
        yield session
    engine.dispose()


def add_trip(session, day, train_id=1, path="path"):
    session.add(
        Trip(
            train_id=train_id,
            service_date=day.isoformat(),
            source_run_key=f"{train_id}:{day}",
            path_sha1=path,
        )
    )
    session.flush()


def test_weekday_profile_window_and_old_trips(session):
    last = date(2026, 9, 27)
    for offset in range(28):
        day = last - timedelta(days=offset)
        if day.weekday() < 5:
            add_trip(session, day)
    # Anchor the window to Sunday without adding a run for train 1.
    session.add(Train(id=2, identity_key="train2"))
    session.flush()
    add_trip(session, last, 2)
    add_trip(session, last - timedelta(days=40))
    profile = load_run_profile(session)
    assert profile.counts[(1, "path")] == (4, 4, 4, 4, 4, 0, 0)
    assert (profile.first_date, profile.last_date) == (date(2026, 8, 31), last)


def test_holidays_exclude_full_but_not_partial_days(session):
    for day in (date(2026, 9, 7), date(2026, 9, 8)):
        add_trip(session, day)
    session.add_all(
        [
            PublicHoliday(date="2026-09-07", state="DE-NW", name="Full", partial=0),
            PublicHoliday(date="2026-09-08", state="DE-BY", name="Partial", partial=1),
        ]
    )
    session.flush()
    assert load_run_profile(session).counts[(1, "path")] == (0, 1, 0, 0, 0, 0, 0)


def test_retired_train_is_excluded_and_cutoff_inclusive(session):
    session.add_all(
        [
            Train(id=2, identity_key="retired", last_seen_date="2026-09-12"),
            Train(id=3, identity_key="boundary", last_seen_date="2026-09-13"),
        ]
    )
    session.flush()
    for train_id in (1, 2, 3):
        add_trip(session, date(2026, 9, 27), train_id)
    profile = load_run_profile(session)
    assert (2, "path") not in profile.counts
    assert profile.counts[(3, "path")][6] == 1


def test_sql_weekdays_are_pinned_to_real_dates(session):
    add_trip(session, date(2026, 9, 7))
    add_trip(session, date(2026, 9, 13))
    rows = session.execute(
        text(PROFILE_SQL.format(trip="trip", holiday="public_holiday", train="train")),
        profile_params(session.connection(), "trip", "train"),
    ).all()
    assert [(r.weekday, r.n_runs) for r in rows] == [(0, 1), (6, 1)]


def test_empty_trip_table(session):
    assert profile_params(session.connection(), "trip", "train") is None
    assert load_run_profile(session) == RunProfile({}, None, None)


def test_missing_tables_returns_empty_profile():
    with Session(create_engine("sqlite://")) as session:
        assert load_run_profile(session) == RunProfile({}, None, None)


def test_null_last_seen_dates_still_qualify(session):
    session.get(Train, 1).last_seen_date = None
    add_trip(session, date(2026, 9, 7), path=None)
    assert load_run_profile(session).counts[(1, None)][0] == 1


def test_deployed_profile_takes_precedence(session):
    session.execute(
        text(
            "CREATE TABLE train_variant_weekday (train_id INTEGER, path_sha1 TEXT, weekday INTEGER, n_runs INTEGER)"
        )
    )
    session.execute(
        text("CREATE TABLE running_profile_window (first_date TEXT, last_date TEXT)")
    )
    session.execute(text("INSERT INTO train_variant_weekday VALUES (7, NULL, 6, 4)"))
    session.execute(
        text("INSERT INTO running_profile_window VALUES ('2026-08-31', '2026-09-27')")
    )
    profile = load_run_profile(session)
    assert profile == RunProfile(
        {(7, None): (0, 0, 0, 0, 0, 0, 4)}, date(2026, 8, 31), date(2026, 9, 27)
    )


def test_day_key_uses_boarding_state():
    holidays = {("2026-10-03", "DE-NW")}
    assert day_key(date(2026, 9, 7), None, holidays) == 0
    assert day_key(date(2026, 9, 13), None, holidays) == 6
    assert day_key(date(2026, 10, 3), "DE-NW", holidays) == 6
    assert day_key(date(2026, 10, 3), None, holidays) == 5


def test_variant_choice_threshold_weekday_total_and_stable_tie():
    profile = RunProfile(
        {
            (1, "only_once"): (1, 0, 0, 0, 0, 0, 0),
            (2, "a"): (3, 2, 0, 0, 0, 0, 0),
            (2, "b"): (2, 2, 9, 0, 0, 0, 0),
            (3, None): (2, 0, 0, 0, 0, 0, 0),
            (3, "z"): (2, 0, 0, 0, 0, 0, 0),
        },
        None,
        None,
    )
    chosen = chosen_variants(profile)
    assert chosen[1][0] is NOTHING
    assert chosen[2][0:3] == ["a", "b", "b"]
    assert chosen[3][0] == "z"
    assert chosen[2][6] is NOTHING


def test_profile_parameters_support_raw_sqlite_connection():
    import sqlite3

    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE trip (service_date TEXT)")
        connection.execute("CREATE TABLE train (last_seen_date TEXT)")
        connection.execute("INSERT INTO trip VALUES ('2026-09-27')")
        connection.execute("INSERT INTO train VALUES ('2026-09-27')")
        assert profile_params(connection, "trip", "train") == {
            "first": "2026-08-31",
            "last": "2026-09-27",
            "cutoff": "2026-09-13",
        }
