"""How late each train usually is at each station, on each day of the week.

One row per (station, train, dow), computed from trip_stop. Splitting on the day of
week is what makes the figure usable: the same train number on a Tuesday and on a
Sunday is a different service with different traffic around it, and one mean over
both describes neither. The cost is that samples divide by seven, so a single week
of collection gives one run per row — the rows only start to mean "usually" after
several weeks.

The honesty of the number rests on two further filters, each of which excludes rows
that would otherwise quietly bias it:

* **Only RECORDED delays.** NO_DATA and PREDICTION_ONLY are unknowns, not zeros;
  counting them as on time would reward exactly the gaps in our own coverage.
  CANCELLED rows carry no delay by construction, so they are counted apart rather
  than folded in as a zero — a cancelled train is not a punctual one.
* **is_handover = 0.** Where a run changes number mid-journey the one physical
  stop appears on both sides of the handover, and averaging it twice would weight
  those stations by an accident of numbering.

Whole pairs are recomputed, but only pairs that changed. Means and counts compose
incrementally; medians and p90 do not, and approximating them with a sketch would
buy nothing against a scan that takes well under a second.
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
from dbrel.models import (
    QUALITY_CANCELLED,
    TRUSTED_QUALITIES,
    TrainStationDelay,
    Trip,
    TripStop,
)

__all__ = ["DelayAveragesResult", "run_delay_averages"]


@dataclass(slots=True)
class DelayAveragesResult:
    pairs_considered: int = 0
    pairs_written: int = 0
    pairs_deleted: int = 0
    samples_read: int = 0
    elapsed_s: float = 0.0


def _dow(service_date: str | None) -> int | None:
    """0=Monday..6=Sunday from a 'YYYY-MM-DD' service date, or None if unusable.

    service_date is the planned departure date at the run's origin, so an overnight
    run counts under the day it set out on at every station it reaches — which is
    the grouping a timetable reader expects, and the only one that keeps a single
    run in a single bucket.
    """
    if not service_date:
        return None
    try:
        return date_cls.fromisoformat(service_date).weekday()
    except ValueError:
        return None


def _dirty_pairs(session: Session) -> set[tuple[int, int]]:
    """(station_id, train_id) pairs whose samples may have changed since last run.

    Keyed on trip_stop.last_observed_at, not trip.finalized_at. finalized_at
    governs *gap grading* and is deliberately withheld for six hours after a run
    ends, because a later read can still turn a NO_DATA into a value — but a
    RECORDED delay is already settled when it is written, since the poller only
    records an event once (ct or pt) + 120 s is in the past. Waiting on
    finalized_at would therefore delay every average by six hours and buy no
    accuracy at all: on a fresh database it would produce nothing whatsoever.
    """
    watermark = session.scalar(select(func.max(TrainStationDelay.computed_at))) or 0
    # Two columns with .distinct(), not distinct(tuple_(...)): most backends
    # cannot SELECT from a tuple construct, and SQLAlchemy refuses to compile it.
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
) -> tuple[dict[tuple[int, int, int], Samples], int]:
    query = (
        select(
            TripStop.station_id,
            Trip.train_id,
            Trip.id,
            Trip.service_date,
            TripStop.arr_delay_s,
            TripStop.dep_delay_s,
            TripStop.quality,
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

    # No ORDER BY: there is no index on trip_stop that would serve one, so sorting
    # would force a temp B-tree over the whole scan. Grouping in a dict costs a few
    # tens of MB at this scale and nothing in time.
    samples: dict[tuple[int, int, int], Samples] = defaultdict(new_samples)
    seen = 0
    stream = session.execute(query.execution_options(yield_per=10_000))
    for station_id, train_id, trip_id, service_date, arr, dep, quality in stream:
        if station_id is None or train_id is None:
            continue
        if pairs is not None and (station_id, train_id) not in pairs:
            continue
        dow = _dow(service_date)
        if dow is None:
            continue
        key = (station_id, train_id, dow)
        seen += 1
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
    return samples, seen


def run_delay_averages(
    session: Session,
    *,
    since: str | None = None,
    full: bool = False,
    station_ids: list[int] | None = None,
    now: int | None = None,
    progress=lambda msg: None,
) -> DelayAveragesResult:
    started = time.monotonic()
    now = now or int(time.time())
    result = DelayAveragesResult()

    pairs = None if full else _dirty_pairs(session)
    if pairs is not None and not pairs:
        progress("  nothing to recompute")
        result.elapsed_s = time.monotonic() - started
        return result

    samples, result.samples_read = _collect(session, pairs, station_ids, since)
    result.pairs_considered = len(pairs) if pairs is not None else len(samples)
    attributes = train_attributes(session, {train_id for _, train_id, _ in samples})

    rows = []
    for (station_id, train_id, dow), bucket in samples.items():
        line_id, segment = attributes.get(train_id, (None, None))
        rows.append(
            {
                "station_id": station_id,
                "train_id": train_id,
                "dow": dow,
                "line_id": line_id,
                "segment": segment,
                **metric_row(bucket, now),
            }
        )

    if rows:
        statement = sqlite_insert(TrainStationDelay)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["station_id", "train_id", "dow"],
                set_={
                    column: statement.excluded[column]
                    for column in rows[0]
                    if column not in ("station_id", "train_id", "dow")
                },
            ),
            rows,
        )
        result.pairs_written = len(rows)

    # A group with no qualifying samples left has to lose its row, or the board
    # keeps showing an average for a stop the train no longer makes. Reconciliation
    # is on the full (station, train, dow) key, not the pair: a train that stops
    # running on Sundays only loses its Sunday row.
    #
    # The stored keys are the candidate set either way — deriving it from `pairs`
    # instead would be a set of pairs being subtracted from a set of triples, which
    # matches nothing and would delete every row it looked at.
    #
    # Incrementally we can only check pairs something told us to look at: a pair
    # whose samples have vanished entirely leaves no trace to mark it dirty, so
    # it survives until a --full pass, which reconciles against the stored rows.
    stored = session.execute(
        select(TrainStationDelay.station_id, TrainStationDelay.train_id, TrainStationDelay.dow)
    ).all()
    if pairs is None:
        stale_scope = {(station_id, train_id, dow) for station_id, train_id, dow in stored}
    else:
        stale_scope = {
            (station_id, train_id, dow)
            for station_id, train_id, dow in stored
            if (station_id, train_id) in pairs
        }

    if stale_scope:
        empty = sorted(stale_scope - set(samples))
        for chunk in range(0, len(empty), 500):
            batch = empty[chunk : chunk + 500]
            session.execute(
                delete(TrainStationDelay).where(
                    or_(
                        *[
                            and_(
                                TrainStationDelay.station_id == station_id,
                                TrainStationDelay.train_id == train_id,
                                TrainStationDelay.dow == dow,
                            )
                            for station_id, train_id, dow in batch
                        ]
                    ),
                )
            )
        result.pairs_deleted = len(empty)

    session.flush()
    result.elapsed_s = time.monotonic() - started
    return result
