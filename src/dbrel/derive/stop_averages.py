"""How late each train usually is at each of its stops.

One row per (day_type, train, station, scheduled time), computed from trip_stop.
Read on its own the table reconstructs a journey: the train, its line, every
station it calls at, when it is meant to be there, and how late it actually runs.

Two grouping decisions carry the whole design.

**Monday to Saturday pool together; Sundays and public holidays do not.** Splitting
by weekday — which train_station_delay does, and which this table deliberately does
not — divides every train's samples by seven, so a month of collection leaves four
runs behind each number. Pooling the working days is what lets a figure mean
"usually" after a few weeks. The one split worth keeping is the day type, because a
Sunday is a different timetable rather than the same one on a different day, and a
public holiday runs the Sunday timetable. holidays_de.day_type_of decides.

**The scheduled time at the station is part of the key.** A train number is not one
service: the same number runs 06:34 Monday to Friday and 07:15 on Saturday often
enough to matter, and one mean over both describes neither. Keying on the time
separates them without needing a list of which numbers do it. It is also the only
honest reading of "the 11:00 train" — the trip-level origin time cannot be used,
because trip.supposed_departure_utc is the first *tracked* stop and moves whenever
a backfill hour is missing.

The two filters that keep the number honest are the same as train_station_delay's,
and for the same reasons: only RECORDED delays (NO_DATA is an unknown, not a zero),
and is_handover = 0 (one physical stop must not be counted twice).
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_cls

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.derive._common import (
    WATERMARK_SLACK_S,
    Samples,
    metric_row,
    new_samples,
    train_attributes,
)
from dbrel.gtfs.timeutil import epoch_to_gtfs_sec
from dbrel.holidays_de import day_type_of
from dbrel.models import (
    QUALITY_CANCELLED,
    TRUSTED_QUALITIES,
    TrainStopAverage,
    Trip,
    TripStop,
)

# The four columns of the unique key, in constraint order. Named once so the
# upsert, the excluded-column list and the reconciliation cannot drift apart.
KEY_COLUMNS = ("day_type", "train_id", "station_id", "sched_sec")


@dataclass(slots=True)
class StopAveragesResult:
    groups_considered: int = 0
    rows_written: int = 0
    rows_deleted: int = 0
    samples_read: int = 0
    elapsed_s: float = 0.0


@dataclass(slots=True)
class _Attrs:
    """The non-key facts of a stop, taken from whichever sample was seen last.

    They are functionally determined by the key in practice (a train calls at a
    station at a given time in one position on one line), so last-writer-wins is
    not a real choice between conflicting values.
    """

    stop_sequence: int | None
    sched_kind: str
    sched_arr_sec: int | None
    sched_dep_sec: int | None


def _sched_sec(service_date: str, arr_utc: int | None, dep_utc: int | None) -> tuple | None:
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


def _dirty_pairs(session: Session) -> set[tuple[int, int]]:
    """(station_id, train_id) pairs whose samples may have changed since last run.

    Deliberately coarser than the row key: a pair is cheap to detect and recomputing
    all of its scheduled times costs nothing, whereas detecting a changed time would
    mean reproducing the key derivation in SQL.

    Keyed on trip_stop.last_observed_at rather than trip.finalized_at, for the reason
    spelled out in delay_averages._dirty_pairs: finalized_at is withheld for six
    hours after a run ends, but a RECORDED delay is settled the moment it is written.
    """
    watermark = session.scalar(select(func.max(TrainStopAverage.computed_at))) or 0
    rows = session.execute(
        select(TripStop.station_id, Trip.train_id)
        .join(Trip, Trip.id == TripStop.trip_id)
        .where(
            TripStop.last_observed_at.isnot(None),
            TripStop.last_observed_at >= watermark - WATERMARK_SLACK_S,
        )
        .distinct()
    ).all()
    return {(station_id, train_id) for station_id, train_id in rows}


def _collect(
    session: Session,
    pairs: set[tuple[int, int]] | None,
    station_ids: list[int] | None,
    since: str | None,
) -> tuple[dict[tuple, Samples], dict[tuple, _Attrs], int]:
    query = (
        select(
            TripStop.station_id,
            Trip.train_id,
            Trip.id,
            Trip.service_date,
            TripStop.arr_delay_s,
            TripStop.dep_delay_s,
            TripStop.quality,
            TripStop.supposed_arrival_utc,
            TripStop.supposed_departure_utc,
            TripStop.stop_sequence,
        )
        .join(Trip, Trip.id == TripStop.trip_id)
        .where(
            TripStop.is_handover == 0,
            TripStop.quality.in_((*TRUSTED_QUALITIES, QUALITY_CANCELLED)),
        )
    )
    if station_ids:
        query = query.where(TripStop.station_id.in_(station_ids))
    if since:
        query = query.where(Trip.service_date >= since)

    samples: dict[tuple, Samples] = defaultdict(new_samples)
    attrs: dict[tuple, _Attrs] = {}
    seen = 0
    stream = session.execute(query.execution_options(yield_per=10_000))
    for (
        station_id,
        train_id,
        trip_id,
        service_date,
        arr,
        dep,
        quality,
        sup_arr,
        sup_dep,
        stop_sequence,
    ) in stream:
        if station_id is None or train_id is None:
            continue
        if pairs is not None and (station_id, train_id) not in pairs:
            continue
        day_type = day_type_of(service_date)
        if day_type is None:
            continue
        scheduled = _sched_sec(service_date, sup_arr, sup_dep)
        if scheduled is None:
            continue
        sched_sec, sched_kind, sched_arr, sched_dep = scheduled

        key = (day_type, train_id, station_id, sched_sec)
        seen += 1
        attrs[key] = _Attrs(stop_sequence, sched_kind, sched_arr, sched_dep)
        bucket = samples[key]
        bucket.trips.add(trip_id)
        bucket.known += 1
        bucket.observe_date(service_date)
        if quality == QUALITY_CANCELLED:
            bucket.cancelled += 1
            continue
        if arr is not None:
            bucket.arr.append(arr)
        if dep is not None:
            bucket.dep.append(dep)
    return samples, attrs, seen


def run_stop_averages(
    session: Session,
    *,
    since: str | None = None,
    full: bool = False,
    station_ids: list[int] | None = None,
    now: int | None = None,
    progress=lambda msg: None,
) -> StopAveragesResult:
    started = time.monotonic()
    now = now or int(time.time())
    result = StopAveragesResult()

    pairs = None if full else _dirty_pairs(session)
    if pairs is not None and not pairs:
        progress("  nothing to recompute")
        result.elapsed_s = time.monotonic() - started
        return result

    samples, attrs, result.samples_read = _collect(session, pairs, station_ids, since)
    result.groups_considered = len(pairs) if pairs is not None else len(samples)
    attributes = train_attributes(session, {train_id for _, train_id, _, _ in samples})

    rows = []
    for key, bucket in samples.items():
        day_type, train_id, station_id, sched_sec = key
        line_id, segment = attributes.get(train_id, (None, None))
        extra = attrs[key]
        rows.append(
            {
                "day_type": day_type,
                "train_id": train_id,
                "station_id": station_id,
                "sched_sec": sched_sec,
                "sched_kind": extra.sched_kind,
                "sched_arr_sec": extra.sched_arr_sec,
                "sched_dep_sec": extra.sched_dep_sec,
                "stop_sequence": extra.stop_sequence,
                "line_id": line_id,
                "segment": segment,
                **metric_row(bucket, now),
            }
        )

    if rows:
        statement = sqlite_insert(TrainStopAverage)
        # Chunked: one row is ~25 columns, and SQLite caps bound variables per
        # statement, so a full rebuild of tens of thousands of rows cannot go in one.
        for chunk in range(0, len(rows), 2000):
            batch = rows[chunk : chunk + 2000]
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=list(KEY_COLUMNS),
                    set_={
                        column: statement.excluded[column]
                        for column in batch[0]
                        if column not in KEY_COLUMNS
                    },
                ),
                batch,
            )
        result.rows_written = len(rows)

    # A group with no qualifying samples left has to lose its row, or the table keeps
    # asserting an average for a call the train no longer makes.
    #
    # The stored keys are the candidate set, not `pairs`: the dirty set is a set of
    # (station, train) pairs and the row key is a 4-tuple, so subtracting one from
    # the other matches nothing and would delete every row it looked at.
    #
    # Incrementally we can only reconcile groups something told us to look at — a
    # group whose samples vanished entirely leaves nothing behind to mark it dirty,
    # so it survives until a --full pass.
    stored = session.execute(
        select(
            TrainStopAverage.day_type,
            TrainStopAverage.train_id,
            TrainStopAverage.station_id,
            TrainStopAverage.sched_sec,
        )
    ).all()
    if pairs is None:
        stale_scope = {tuple(row) for row in stored}
    else:
        stale_scope = {tuple(row) for row in stored if (row[2], row[1]) in pairs}

    if stale_scope:
        empty = sorted(stale_scope - set(samples))
        for chunk in range(0, len(empty), 500):
            batch = empty[chunk : chunk + 500]
            session.execute(
                delete(TrainStopAverage).where(
                    or_(
                        *[
                            and_(
                                TrainStopAverage.day_type == day_type,
                                TrainStopAverage.train_id == train_id,
                                TrainStopAverage.station_id == station_id,
                                TrainStopAverage.sched_sec == sched_sec,
                            )
                            for day_type, train_id, station_id, sched_sec in batch
                        ]
                    ),
                )
            )
        result.rows_deleted = len(empty)

    session.flush()
    result.elapsed_s = time.monotonic() - started
    return result
