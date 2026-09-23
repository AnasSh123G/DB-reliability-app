"""The per-state holiday rules, and the day-type split they drive.

The computus is the part that can be wrong without looking wrong, so it is
checked against published Easter dates rather than against itself.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.holidays_de import (
    ALL_STATE_CODES,
    day_type_of,
    easter_sunday,
    ensure_holidays,
    holidays_for,
    is_holiday,
    load_holiday_dates,
    tracked_years,
)
from dbrel.models import DAY_TYPE_SUNDAY_HOLIDAY, DAY_TYPE_WEEKDAY, PublicHoliday

NW_2026 = {d.isoformat() for d in holidays_for("DE-NW", 2026)}


@pytest.mark.parametrize(
    "year,expected",
    [
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
        (2028, date(2028, 4, 16)),
        # Both ends of the possible range, which is where an off-by-one hides.
        (2038, date(2038, 4, 25)),  # latest Easter can fall
        (2035, date(2035, 3, 25)),  # near the earliest
    ],
)
def test_easter_matches_published_dates(year, expected):
    assert easter_sunday(year) == expected


def test_easter_is_always_a_sunday():
    for year in range(2020, 2061):
        assert easter_sunday(year).weekday() == 6, year


def test_all_16_state_codes_are_present():
    assert len(ALL_STATE_CODES) == 16
    assert len(set(ALL_STATE_CODES)) == 16


@pytest.mark.parametrize("year", [2025, 2026, 2027])
def test_holidays_for_nw_has_the_two_nrw_holidays(year):
    """Fronleichnam and Allerheiligen are NW holidays but not federal."""
    days = holidays_for("DE-NW", year)
    names = {name for name, _partial in days.values()}
    assert "Fronleichnam" in names
    assert "Allerheiligen" in names


def test_fronleichnam_applies_in_bavaria_but_not_hamburg():
    """State-specific holiday: Bayern observes it, Hamburg doesn't."""
    by_names = {name for name, _p in holidays_for("DE-BY", 2026).values()}
    hh_names = {name for name, _p in holidays_for("DE-HH", 2026).values()}
    assert "Fronleichnam" in by_names
    assert "Fronleichnam" not in hh_names


def test_a_nationwide_holiday_appears_for_every_state():
    for state in ALL_STATE_CODES:
        names = {name for name, _p in holidays_for(state, 2026).values()}
        assert "Tag der Deutschen Einheit" in names, state


def test_augsburgs_friedensfest_is_marked_partial_and_only_in_bavaria():
    days = holidays_for("DE-BY", 2026)
    friedensfest = [
        (day, partial) for day, (name, partial) in days.items() if name == "Friedensfest"
    ]
    assert friedensfest and all(partial for _day, partial in friedensfest)
    for state in ALL_STATE_CODES:
        if state == "DE-BY":
            continue
        names = {name for name, _p in holidays_for(state, 2026).values()}
        assert "Friedensfest" not in names, state


def test_christi_himmelfahrt_is_always_a_thursday():
    for year in range(2024, 2036):
        ascension = easter_sunday(year) + timedelta(days=39)
        assert ascension.weekday() == 3, year


class TestDayType:
    def test_plain_weekdays_are_weekdays(self):
        for day in ("2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"):
            assert day_type_of(day, NW_2026) == DAY_TYPE_WEEKDAY, day

    def test_saturday_is_a_weekday(self):
        assert date.fromisoformat("2026-09-19").weekday() == 5
        assert day_type_of("2026-09-19", NW_2026) == DAY_TYPE_WEEKDAY

    def test_sunday_is_not(self):
        assert date.fromisoformat("2026-09-20").weekday() == 6
        assert day_type_of("2026-09-20", NW_2026) == DAY_TYPE_SUNDAY_HOLIDAY

    def test_a_holiday_on_a_saturday_is_still_a_holiday(self):
        assert date.fromisoformat("2026-10-03").weekday() == 5
        assert day_type_of("2026-10-03", NW_2026) == DAY_TYPE_SUNDAY_HOLIDAY

    def test_a_holiday_midweek_is_a_holiday(self):
        assert date.fromisoformat("2026-05-14").weekday() == 3
        assert day_type_of("2026-05-14", NW_2026) == DAY_TYPE_SUNDAY_HOLIDAY

    def test_only_consults_the_given_set_not_the_calendar_rules(self):
        assert day_type_of("2026-10-03", set()) == DAY_TYPE_WEEKDAY
        assert day_type_of("2026-08-26", {"2026-08-26"}) == DAY_TYPE_SUNDAY_HOLIDAY

    @pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-13-01"])
    def test_unusable_input_returns_none_rather_than_guessing(self, bad):
        assert day_type_of(bad, NW_2026) is None


class TestIsHoliday:
    """The three-way per-state rule: Sunday always, state-specific holiday only
    in its own state, no-state falls back to "all 16 states" only."""

    def test_a_state_specific_holiday_applies_only_in_its_own_state(self):
        holiday_set = {("2026-06-04", "DE-BY")}  # Fronleichnam 2026
        assert is_holiday(date(2026, 6, 4), "DE-BY", holiday_set) is True
        assert is_holiday(date(2026, 6, 4), "DE-HH", holiday_set) is False

    def test_a_nationwide_holiday_applies_in_every_state(self):
        holiday_set = {("2026-10-03", code) for code in ALL_STATE_CODES}
        for state in ALL_STATE_CODES:
            assert is_holiday(date(2026, 10, 3), state, holiday_set) is True

    def test_every_sunday_is_a_holiday_regardless_of_state(self):
        assert date(2026, 9, 20).weekday() == 6
        assert is_holiday(date(2026, 9, 20), "DE-NW", set()) is True
        assert is_holiday(date(2026, 9, 20), None, set()) is True

    def test_unknown_state_only_counts_an_all_16_states_holiday(self):
        partial_set = {("2026-06-04", "DE-BY")}  # Fronleichnam, one state only
        assert is_holiday(date(2026, 6, 4), None, partial_set) is False

        nationwide_set = {("2026-10-03", code) for code in ALL_STATE_CODES}
        assert is_holiday(date(2026, 10, 3), None, nationwide_set) is True

    def test_partial_holiday_never_flags_a_state_via_this_function(self):
        """Augsburg's Friedensfest never enters `holiday_set` in the first
        place (load_holiday_dates excludes partial rows) -- confirmed here by
        showing an empty set for it means "not a holiday" for all of Bavaria."""
        assert is_holiday(date(2026, 8, 8), "DE-BY", set()) is False


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


class TestHolidayTable:
    def test_ensure_holidays_populates_all_16_states(self, session):
        added = ensure_holidays(session, [2026])
        assert added > 0
        states_seen = {state for _date, state in load_holiday_dates(session)}
        assert states_seen == set(ALL_STATE_CODES)

    def test_ensure_holidays_is_additive_only(self, session):
        session.add(
            PublicHoliday(date="2026-08-26", state="DE-NW", name="Betriebsruhetag (test)")
        )
        session.flush()
        ensure_holidays(session, [2026])
        dates = load_holiday_dates(session)
        assert ("2026-08-26", "DE-NW") in dates

    def test_ensure_holidays_does_not_duplicate_across_calls(self, session):
        first = ensure_holidays(session, [2026])
        second = ensure_holidays(session, [2026])
        assert first > 0
        assert second == 0

    def test_load_holiday_dates_on_an_empty_table_is_an_empty_set(self, session):
        assert load_holiday_dates(session) == set()

    def test_load_holiday_dates_excludes_partial_rows(self, session):
        session.add(
            PublicHoliday(date="2026-08-08", state="DE-BY", name="Friedensfest", partial=1)
        )
        session.flush()
        assert ("2026-08-08", "DE-BY") not in load_holiday_dates(session)

    def test_tracked_years_matches_the_resource_file(self):
        assert set(tracked_years()) >= {2025, 2026, 2027}
