"""GTFS clock times <-> absolute UTC instants.

GTFS stop times are *not* wall-clock times on a calendar day. They are offsets from
"noon minus 12 hours" of the service date, which is why values like 25:30:00 are legal
and why a service date is not the same thing as a calendar date.

The noon anchor matters exactly twice a year. On 2026-03-29 Europe/Berlin skips
02:00-03:00, so the service day is 23 hours long and its anchor is 23:00 local on the
previous day; on 2026-10-25 the day is 25 hours long. Anchoring at midnight instead
would silently shift every time on those days by an hour.

All absolute instants in this project are INTEGER Unix epoch seconds.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta

from dbrel.config import TZ

DAY_S = 86_400
_NOON_S = 12 * 3600


def parse_gtfs_time(value: str | None) -> int | None:
    """'25:30:00' -> 91800. Returns None for empty or malformed input.

    GTFS permits hours beyond 24 and does not require zero padding.
    """
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    if m < 0 or m > 59 or s < 0 or s > 59 or h < 0:
        return None
    return h * 3600 + m * 60 + s


def format_gtfs_time(seconds: int) -> str:
    """91800 -> '25:30:00'. Inverse of parse_gtfs_time."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_service_date(value: str) -> date:
    """'20260910' or '2026-09-10' -> date."""
    value = value.strip()
    fmt = "%Y%m%d" if len(value) == 8 and value.isdigit() else "%Y-%m-%d"
    return datetime.strptime(value, fmt).date()


def service_date_anchor(service_date: date) -> int:
    """Epoch seconds of 'noon minus 12h' local time on `service_date`.

    Noon is used because it always exists and is never ambiguous, unlike midnight
    on a DST transition day.
    """
    noon = datetime.combine(service_date, time(12), tzinfo=TZ)
    return int(noon.timestamp()) - _NOON_S


def gtfs_sec_to_epoch(service_date: date, seconds: int) -> int:
    """Absolute instant of a GTFS clock time on a given service date."""
    return service_date_anchor(service_date) + seconds


def epoch_to_gtfs_sec(service_date: date, epoch: int) -> int:
    """Inverse of gtfs_sec_to_epoch. May exceed 86400, or go negative."""
    return epoch - service_date_anchor(service_date)


def read_clock(frozen: int | None) -> int:
    """The instant an observation should be judged against.

    Live, that is the wall clock, read afresh at each use so a long sweep stamps
    each station with its own fetch time. Replaying an archived response, it is
    the instant that response was recorded: the caller freezes it, because a
    response read months later is still only evidence of what was known then.

    Left at wall clock, a replay would find every archived event long past, so
    every IRIS forecast would grade RECORDED -- the defect that made the upstream
    project's processed data unusable. See openspec/changes/add-archive-backfill.
    """
    return int(_time.time()) if frozen is None else frozen


def local_hour(epoch: int) -> int:
    """Local clock hour (0-23) of an instant, for hour-of-day aggregation."""
    return datetime.fromtimestamp(epoch, TZ).hour


def local_date(epoch: int) -> date:
    """Local calendar date of an instant."""
    return datetime.fromtimestamp(epoch, TZ).date()


def service_dates_for_instant(epoch: int) -> tuple[date, date]:
    """The two service dates a given instant could plausibly belong to.

    A trip departing 23:50 on service date D can still be running at 01:10 the next
    calendar day, so an observation timestamp maps to either D or D-1. Used when a
    realtime descriptor omits start_date.
    """
    today = local_date(epoch)
    return today, today - timedelta(days=1)


def date_range(start: date, end: date):
    """Inclusive date iterator."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def sched_sec_of(service_date: str, arr_utc: int | None, dep_utc: int | None) -> tuple | None:
    """(sched_sec, kind, arr_sec, dep_sec) in service-day seconds, or None.

    Departure preferred, arrival as fallback: keying on the departure alone would
    leave every terminus with a NULL key, and SQLite treats NULLs in a unique
    constraint as distinct — each terminus row would insert afresh on every run
    instead of upserting over itself.

    Converted through epoch_to_gtfs_sec rather than a local clock hour so the value
    is stable across the two DST days; it may legitimately exceed 86400 for a stop
    after midnight.
    """
    try:
        day = date.fromisoformat(service_date)
    except (ValueError, TypeError):
        return None
    arr_sec = epoch_to_gtfs_sec(day, arr_utc) if arr_utc is not None else None
    dep_sec = epoch_to_gtfs_sec(day, dep_utc) if dep_utc is not None else None
    if dep_sec is not None:
        return dep_sec, "DEP", arr_sec, dep_sec
    if arr_sec is not None:
        return arr_sec, "ARR", arr_sec, dep_sec
    return None
