"""Berlin-local 'HH:MM' of a UTC epoch column, expressed as a SQL CASE.

`resolve_day_type`/`local_date` etc. do this in Python, one row at a time --
fine for a web request, wrong for a 14.9M-row aggregation. This computes the
actual CEST/CET transition instants for whichever years the data spans (via
`zoneinfo`, so it reflects the real German DST rule rather than a hand-picked
"last Sunday of March/October" guess, which is right today but is exactly the
kind of rule that's cheaper to ask the tz database for than to re-derive),
and turns them into a `CASE WHEN epoch < ... THEN epoch + offset ...` block
SQLite can evaluate per row without ever calling 'localtime'.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")


def _utcoffset_at(epoch: int) -> int:
    return int(datetime.fromtimestamp(epoch, tz=TZ).utcoffset().total_seconds())


def _year_bounds_epoch(year: int) -> tuple[int, int]:
    start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    return start, end


def dst_transitions(year: int) -> list[int]:
    """Epoch seconds of every UTC-offset change within `year` (0, 1, or 2 of them)."""
    start, end = _year_bounds_epoch(year)
    transitions: list[int] = []
    prev_off = _utcoffset_at(start)
    t = start
    step = 86400
    while t < end:
        nxt = min(t + step, end)
        off = _utcoffset_at(nxt)
        if off != prev_off:
            lo, hi = t, nxt
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if _utcoffset_at(mid) == prev_off:
                    lo = mid
                else:
                    hi = mid
            transitions.append(hi)
        prev_off = off
        t = nxt
    return transitions


def offset_breakpoints(years: list[int]) -> list[tuple[int, int]]:
    """[(breakpoint_epoch, utc_offset_seconds_from_there_on), ...], sorted.

    Covers `years` plus one year of padding on each side so a stop right at a
    year boundary is never left outside the computed range.
    """
    if not years:
        years = [datetime.now(tz=timezone.utc).year]
    span = range(min(years) - 1, max(years) + 2)
    all_transitions: list[int] = []
    for year in span:
        all_transitions.extend(dst_transitions(year))
    all_transitions.sort()
    first_year_start, _ = _year_bounds_epoch(min(span))
    breakpoints = [(first_year_start, _utcoffset_at(first_year_start))]
    for epoch in all_transitions:
        breakpoints.append((epoch, _utcoffset_at(epoch)))
    return breakpoints


def local_hhmm_case_sql(column: str, years: list[int]) -> str:
    """A SQL expression: Berlin-local 'HH:MM' of the epoch in `column`.

    `column` is inlined verbatim (e.g. 'ts.supposed_departure_utc') -- caller's
    responsibility to pass a trusted identifier, never user input.
    """
    breakpoints = offset_breakpoints(years)
    # breakpoints[0] is (start_of_range, offset); everything before it never
    # occurs in real data, so its own offset is used as the fallback for "before
    # the first breakpoint" too.
    cases = []
    for i in range(1, len(breakpoints)):
        boundary_epoch, _next_offset = breakpoints[i]
        offset_before = breakpoints[i - 1][1]
        cases.append(f"WHEN {column} < {boundary_epoch} THEN {column} + {offset_before}")
    last_offset = breakpoints[-1][1]
    case_sql = "CASE " + " ".join(cases) + f" ELSE {column} + {last_offset} END"
    return f"strftime('%H:%M', ({case_sql}), 'unixepoch')"
