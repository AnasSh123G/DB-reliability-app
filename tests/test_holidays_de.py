"""The per-state holiday rules, and the day-type split they drive.

The computus is the part that can be wrong without looking wrong, so it is
checked against published Easter dates rather than against itself.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.holidays_de import (
    ALL_STATE_CODES,
    easter_sunday,
    ensure_holidays,
    holidays_for,
    is_holiday,
    load_holiday_dates,
)

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


def test_fronleichnam_applies_in_bavaria_but_not_hamburg():
    """State-specific holiday: Bayern observes it, Hamburg doesn't."""
    by_names = {name for name, _p in holidays_for("DE-BY", 2026).values()}
    hh_names = {name for name, _p in holidays_for("DE-HH", 2026).values()}
    assert "Fronleichnam" in by_names
    assert "Fronleichnam" not in hh_names


class TestIsHoliday:
    """The three-way per-state rule: Sunday always, state-specific holiday only
    in its own state, no-state falls back to "all 16 states" only."""


    def test_every_sunday_is_a_holiday_regardless_of_state(self):
        assert date(2026, 9, 20).weekday() == 6
        assert is_holiday(date(2026, 9, 20), "DE-NW", set()) is True
        assert is_holiday(date(2026, 9, 20), None, set()) is True


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


