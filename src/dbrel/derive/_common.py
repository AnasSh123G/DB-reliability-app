"""Pieces shared between the derive layer and the ML pipeline.

Both `sched_sec_of` and `percentile` are *keys and statistics*, not aggregation
machinery — the accumulator and row-builder that used to live alongside them
were removed with train_station_delay and train_stop_average, the two
aggregate tables that used to be built from them. What is left is shared
because it must never be computed two different ways: the model's feature
extraction and its baseline evaluation both need the exact same scheduled-time
key and the exact same percentile a stored aggregate once used, or the numbers
they produce would silently stop meaning the same thing.
"""

from __future__ import annotations

from datetime import date as date_cls

from dbrel.gtfs.timeutil import epoch_to_gtfs_sec


def sched_sec_of(service_date: str, arr_utc: int | None, dep_utc: int | None) -> tuple | None:
    """(sched_sec, kind, arr_sec, dep_sec) in service-day seconds, or None.

    Departure preferred, arrival as fallback: keying on the departure alone would
    leave every terminus with a NULL key, and SQLite treats NULLs in a unique
    constraint as distinct — each terminus row would insert afresh on every run
    instead of upserting over itself.

    Converted through epoch_to_gtfs_sec rather than a local clock hour so the value
    is stable across the two DST days; it may legitimately exceed 86400 for a stop
    after midnight.

    Lives here because it is a *key*, and ml/features.py must derive it
    identically every time or two runs of the same model would disagree about
    what a given call even is.
    """
    try:
        day = date_cls.fromisoformat(service_date)
    except (ValueError, TypeError):
        return None
    arr_sec = epoch_to_gtfs_sec(day, arr_utc) if arr_utc is not None else None
    dep_sec = epoch_to_gtfs_sec(day, dep_utc) if dep_utc is not None else None
    if dep_sec is not None:
        return dep_sec, "DEP", arr_sec, dep_sec
    if arr_sec is not None:
        return arr_sec, "ARR", arr_sec, dep_sec
    return None


def percentile(ordered: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile over an already-sorted list.

    SQLite has no percentile_cont, and sorting each group's samples in Python is
    exact, single-pass and needs no extension. Used by ml/evaluate.py to rebuild
    the historical-average baseline the model is scored against.
    """
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))
    return ordered[index]
