"""German public holidays for Nordrhein-Westfalen, and the day-type split.

A public holiday runs a Sunday timetable. Averaging one into the Monday-to-Saturday
figure would quietly mix two different services, and on a holiday that falls midweek
— Fronleichnam is always a Thursday, Christi Himmelfahrt always a Thursday — the
weekday average would be dragged by a day that was never a working day at all.

NRW rather than Germany: holidays are set per Bundesland, and the tracked stations
are all in the Ruhr. Fronleichnam and Allerheiligen are NRW holidays but not
national ones, so using a federal list would misclassify two days a year.

Hand-rolled rather than a dependency. The rule set is eleven days, seven of them
fixed and four derived from one Easter calculation, and the project keeps few
dependencies on purpose (see the note on .env parsing in config.py).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from functools import lru_cache

# Fixed-date NRW holidays: (month, day) -> name.
_FIXED: tuple[tuple[int, int, str], ...] = (
    (1, 1, "Neujahr"),
    (5, 1, "Tag der Arbeit"),
    (10, 3, "Tag der Deutschen Einheit"),
    (11, 1, "Allerheiligen"),
    (12, 25, "1. Weihnachtstag"),
    (12, 26, "2. Weihnachtstag"),
)

# Easter-relative NRW holidays: offset in days from Easter Sunday -> name.
# Karfreitag is the Friday before; the rest follow. Fronleichnam at +60 is the
# Thursday after Trinity, and is the one most likely to be got wrong by hand.
_EASTER_RELATIVE: tuple[tuple[int, str], ...] = (
    (-2, "Karfreitag"),
    (1, "Ostermontag"),
    (39, "Christi Himmelfahrt"),
    (50, "Pfingstmontag"),
    (60, "Fronleichnam"),
)


def easter_sunday(year: int) -> date:
    """Easter Sunday in the Gregorian calendar, by the anonymous computus.

    Meeus/Jones/Butcher. Exact for the whole Gregorian range — no table, no cutoff
    year — which matters because this is the only part of the module that cannot be
    checked by reading it.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lunar = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lunar) // 451
    month, day = divmod(h + lunar - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=None)
def nrw_holidays(year: int) -> dict[date, str]:
    """Every NRW public holiday in `year`, as date -> German name.

    Cached: a full averaging pass classifies one date per trip_stop, which is
    hundreds of thousands of calls across a handful of distinct years.
    """
    easter = easter_sunday(year)
    days = {date(year, month, day): name for month, day, name in _FIXED}
    days.update({easter + timedelta(days=offset): name for offset, name in _EASTER_RELATIVE})
    return days


def is_public_holiday(day: date) -> bool:
    return day in nrw_holidays(day.year)


def holiday_name(day: date) -> str | None:
    return nrw_holidays(day.year).get(day)


def day_type_of(service_date: str | None, holiday_dates: set[str]) -> str | None:
    """'WEEKDAY' or 'SUNDAY_HOLIDAY' for a 'YYYY-MM-DD' date, None if unusable.

    Saturday counts as a weekday here. It runs a reduced timetable, but it is a
    working day and the request was to pool everything except Sundays and holidays.

    `holiday_dates` is the public_holiday table's contents (load_holiday_dates),
    not a recomputation of the NRW rules: the table is what the app actually
    checks, so a holiday that only exists as a row -- added by hand, not derived
    from any calendar rule -- is honoured exactly like a real one.

    The caller passes trip.service_date, which is the planned departure date at the
    run's ORIGIN — so an overnight run keeps one day type at every station it
    reaches, instead of splitting itself across midnight.
    """
    if not service_date:
        return None
    try:
        day = date.fromisoformat(service_date)
    except ValueError:
        return None
    # Imported here rather than at module scope: models imports config imports
    # nothing from here, but keeping this module import-light means the holiday
    # rules can be unit-tested without pulling in SQLAlchemy.
    from dbrel.models import DAY_TYPE_SUNDAY_HOLIDAY, DAY_TYPE_WEEKDAY

    if day.weekday() == 6 or service_date in holiday_dates:
        return DAY_TYPE_SUNDAY_HOLIDAY
    return DAY_TYPE_WEEKDAY


def load_holiday_dates(session) -> set[str]:
    """The public_holiday table's dates, as the 'YYYY-MM-DD' strings it stores them in.

    One query per averaging run rather than one per row: day_type_of is called
    once per trip_stop, and a full pass is hundreds of thousands of rows.
    """
    from sqlalchemy import select

    from dbrel.models import PublicHoliday

    return set(session.execute(select(PublicHoliday.date)).scalars())


def ensure_holidays(session, years: Iterable[int]) -> int:
    """Insert this module's NRW holidays for `years` into public_holiday.

    Additive only -- it never updates or deletes a row that is already there, so
    a holiday added by hand (a Betriebsruhetag, a one-off test date, anything not
    on the NRW calendar) is never overwritten by the next automated run. Returns
    how many rows were added.
    """
    from dbrel.models import PublicHoliday

    existing = load_holiday_dates(session)
    added = 0
    for year in years:
        for day, name in nrw_holidays(year).items():
            iso = day.isoformat()
            if iso in existing:
                continue
            session.add(PublicHoliday(date=iso, name=name))
            existing.add(iso)
            added += 1
    return added
