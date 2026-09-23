"""German public holidays, per Bundesland, and the day-type split.

A public holiday runs a Sunday timetable. Averaging one into the Monday-to-
Saturday figure would quietly mix two different services, and a holiday that
falls midweek -- Fronleichnam is always a Thursday, Christi Himmelfahrt always
a Thursday -- would drag the weekday average with a day that was never a
working day at all.

Per state, not nationwide: German holidays are set per Bundesland (16 of
them), not federally. Fronleichnam is a holiday in Bayern but not in Hamburg,
so a single nationwide list -- what this module used to be, NRW-only, back
when tracking was Ruhr-only -- misclassifies every non-NRW station once
tracking goes national (`[[dbrel-national-collection]]`).

Fetched, not hand-rolled. The old NRW ruleset was hand-rolled because eleven
days, seven fixed and four Easter-relative, is small enough to get right by
inspection. Sixteen states' worth includes municipality-level exceptions
(Friedensfest is Augsburg only, inside Bayern) that are easy to get subtly
wrong by hand; `resources/holidays_by_state.json` is fetched from an external
per-state source instead and checked into the repo as a static asset, so this
module only has to load it. `easter_sunday` is kept because Sunday-ness of
Easter itself is still worth an offline check, not because it derives the
holiday list anymore.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, timedelta
from functools import lru_cache
from importlib import resources

# The 16 Bundesland ISO 3166-2:DE codes, in the order resources/de_states.geojson
# and resources/holidays_by_state.json both use.
ALL_STATE_CODES: tuple[str, ...] = (
    "DE-BW", "DE-BY", "DE-BE", "DE-BB", "DE-HB", "DE-HH", "DE-HE", "DE-MV",
    "DE-NI", "DE-NW", "DE-RP", "DE-SL", "DE-SN", "DE-ST", "DE-SH", "DE-TH",
)


def easter_sunday(year: int) -> date:
    """Easter Sunday in the Gregorian calendar, by the anonymous computus.

    Meeus/Jones/Butcher. Exact for the whole Gregorian range -- no table, no
    cutoff year -- and checked against published dates in the test suite,
    since it's the one part of this module that can't be checked by reading
    it.
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


@lru_cache(maxsize=1)
def _raw() -> dict:
    text = resources.files("dbrel.resources").joinpath("holidays_by_state.json").read_text()
    return json.loads(text)


@lru_cache(maxsize=None)
def holidays_for(state: str, year: int) -> dict[date, tuple[str, bool]]:
    """Every `public_holiday` entry for `state` in `year`: date -> (name, partial).

    `partial` means a city/region-only observance within the state (currently
    only Augsburg's Friedensfest, scope DE-BY-AU) -- true for it, false for an
    ordinary statewide holiday. Cached: this is read once per (state, year)
    pair across a whole run, not per row.
    """
    out: dict[date, tuple[str, bool]] = {}
    for entry in _raw()["states"].get(state, []):
        day = date.fromisoformat(entry["date"])
        if day.year != year:
            continue
        out[day] = (entry["name"], bool(entry.get("partial", False)))
    return out


def tracked_years() -> list[int]:
    """The years resources/holidays_by_state.json has data for."""
    return list(_raw().get("years", []))


def day_type_of(service_date: str | None, holiday_dates: set[str]) -> str | None:
    """'WEEKDAY' or 'SUNDAY_HOLIDAY' for a 'YYYY-MM-DD' date, None if unusable.

    Saturday counts as a weekday here. It runs a reduced timetable, but it is a
    working day and the request was to pool everything except Sundays and
    holidays.

    `holiday_dates` is a plain set of 'YYYY-MM-DD' strings -- already resolved
    to "this date counts as a holiday for the case the caller cares about"
    (one state, or "observed in all 16 states" when no station is known; see
    `resolve_day_type` in web/queries.py). This function does not itself know
    about states; it only tests Sunday-or-in-the-set, same as before the
    per-state rewrite.

    The caller passes trip.service_date, which is the planned departure date at
    the run's ORIGIN -- so an overnight run keeps one day type at every station
    it reaches, instead of splitting itself across midnight.
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


def is_holiday(day: date, state: str | None, holiday_set: set[tuple[str, str]]) -> bool:
    """Whether `day` is a holiday-timetable day for a station in `state`.

    Every Sunday is a holiday everywhere, regardless of state. Otherwise:
    - `state` given: True iff (day, state) is a non-partial row in `holiday_set`.
    - `state` is None (station's state unresolved): True only if `day` is a
      holiday in *all* 16 states -- the one case an unknown station can still
      be answered for without guessing. This is the rule `resolve_day_type`
      uses, which has no "unknown" bucket to fall back to; the stricter,
      three-valued version of this rule (True/False/None) used for labelling
      `trip_stop.is_holiday` lives in `derive/stop_averages.py` and is computed
      in SQL, not through this function, since it runs over 14M+ rows.

    `holiday_set` is `load_holiday_dates(session)`'s output: (date, state)
    pairs for non-partial rows only. Partial (city-only) holidays never make it
    into this set, so they can never flag a whole state via this function.
    """
    if day.weekday() == 6:
        return True
    iso = day.isoformat()
    if state is not None:
        return (iso, state) in holiday_set
    return all((iso, code) in holiday_set for code in ALL_STATE_CODES)


def load_holiday_dates(session) -> set[tuple[str, str]]:
    """The public_holiday table's (date, state) pairs for non-partial rows.

    Partial (city-only) rows are excluded here on purpose: nothing in this
    codebase resolves a station down to city level, only to Bundesland, so a
    partial holiday can never be correctly attributed to "this station is/isn't
    observing it" and must never flag a whole state.

    One query per run rather than one per row: `is_holiday`/`day_type_of` are
    called per trip_stop in the old per-row paths, and a full pass is hundreds
    of thousands of rows.
    """
    from sqlalchemy import select

    from dbrel.models import PublicHoliday

    rows = session.execute(
        select(PublicHoliday.date, PublicHoliday.state).where(PublicHoliday.partial == 0)
    ).all()
    return {(d, s) for d, s in rows}


def ensure_holidays(session, years: Iterable[int]) -> int:
    """Insert every state's holidays for `years` into public_holiday.

    Additive only -- it never updates or deletes a row that is already there,
    so a holiday added by hand (a Betriebsruhetag, a one-off test date,
    anything not in the fetched data) is never overwritten by the next
    automated run. Returns how many rows were added.
    """
    from sqlalchemy import select

    from dbrel.models import PublicHoliday

    existing: set[tuple[str, str]] = set(
        session.execute(select(PublicHoliday.date, PublicHoliday.state)).all()
    )
    added = 0
    for year in years:
        for state in ALL_STATE_CODES:
            for day, (name, partial) in holidays_for(state, year).items():
                iso = day.isoformat()
                key = (iso, state)
                if key in existing:
                    continue
                session.add(
                    PublicHoliday(date=iso, state=state, name=name, partial=int(partial))
                )
                existing.add(key)
                added += 1
    return added
