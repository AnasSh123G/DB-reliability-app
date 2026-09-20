"""The holiday rules, and the day-type split they drive.

The computus is the part that can be wrong without looking wrong, so it is checked
against published Easter dates rather than against itself. Everything else follows
from it by a fixed offset.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dbrel.holidays_de import (
    day_type_of,
    easter_sunday,
    holiday_name,
    is_public_holiday,
    nrw_holidays,
)
from dbrel.models import DAY_TYPE_SUNDAY_HOLIDAY, DAY_TYPE_WEEKDAY


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


@pytest.mark.parametrize("year", [2025, 2026, 2027])
def test_moveable_feasts_sit_at_their_fixed_offsets(year):
    easter = easter_sunday(year)
    days = nrw_holidays(year)
    assert days[easter - timedelta(days=2)] == "Karfreitag"
    assert days[easter + timedelta(days=1)] == "Ostermontag"
    assert days[easter + timedelta(days=39)] == "Christi Himmelfahrt"
    assert days[easter + timedelta(days=50)] == "Pfingstmontag"
    assert days[easter + timedelta(days=60)] == "Fronleichnam"


@pytest.mark.parametrize("year", [2025, 2026, 2027, 2030])
def test_nrw_has_eleven_distinct_holidays(year):
    days = nrw_holidays(year)
    assert len(days) == 11
    assert all(day.year == year for day in days)


def test_the_two_nrw_only_holidays_are_present():
    """Fronleichnam and Allerheiligen are NRW, not federal.

    Using a Germany-wide list instead would misclassify two days a year.
    """
    days = nrw_holidays(2026)
    assert "Fronleichnam" in days.values()
    assert days[date(2026, 11, 1)] == "Allerheiligen"


def test_christi_himmelfahrt_is_always_a_thursday():
    """The reason the weekday split alone is not enough.

    A midweek holiday runs a Sunday timetable on what date arithmetic calls a
    Thursday, so pooling by weekday would drag the Thursday average.
    """
    for year in range(2024, 2036):
        ascension = easter_sunday(year) + timedelta(days=39)
        assert ascension.weekday() == 3, year
        assert is_public_holiday(ascension)


def test_a_normal_day_is_not_a_holiday():
    assert not is_public_holiday(date(2026, 9, 18))
    assert holiday_name(date(2026, 9, 18)) is None


class TestDayType:
    def test_plain_weekdays_are_weekdays(self):
        for day in ("2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"):
            assert day_type_of(day) == DAY_TYPE_WEEKDAY, day

    def test_saturday_is_a_weekday(self):
        """A reduced timetable, but a working day — it pools with Mon-Fri."""
        assert date.fromisoformat("2026-09-19").weekday() == 5
        assert day_type_of("2026-09-19") == DAY_TYPE_WEEKDAY

    def test_sunday_is_not(self):
        assert date.fromisoformat("2026-09-20").weekday() == 6
        assert day_type_of("2026-09-20") == DAY_TYPE_SUNDAY_HOLIDAY

    def test_a_holiday_on_a_saturday_is_still_a_holiday(self):
        """2026-10-03, Tag der Deutschen Einheit, falls on a Saturday."""
        assert date.fromisoformat("2026-10-03").weekday() == 5
        assert day_type_of("2026-10-03") == DAY_TYPE_SUNDAY_HOLIDAY

    def test_a_holiday_midweek_is_a_holiday(self):
        """2026-05-14, Christi Himmelfahrt, falls on a Thursday.

        This is the case the whole module exists for: nothing about the date says
        it is special, and a weekday-only split would average it into Thursdays.
        """
        assert date.fromisoformat("2026-05-14").weekday() == 3
        assert day_type_of("2026-05-14") == DAY_TYPE_SUNDAY_HOLIDAY

    @pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-13-01"])
    def test_unusable_input_returns_none_rather_than_guessing(self, bad):
        assert day_type_of(bad) is None
