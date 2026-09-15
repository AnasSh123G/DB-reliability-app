"""How late each train usually is at each station.

One row per (station, train, basis), computed from trip_stop. The honesty of the
number rests on three filters, each of which excludes rows that would otherwise
quietly bias it:

* **Only RECORDED delays.** NO_DATA and PREDICTION_ONLY are unknowns, not zeros;
  counting them as on time would reward exactly the gaps in our own coverage.
  CANCELLED rows carry no delay by construction, so they are counted apart rather
  than folded in as a zero — a cancelled train is not a punctual one.
* **is_handover = 0.** Where a run changes number mid-journey the one physical
  stop appears on both sides of the handover, and averaging it twice would weight
  those stations by an accident of numbering.
* **Real and simulated never share a row.** basis is part of the unique key, so no
  average is ever a blend of the two.

Whole pairs are recomputed, but only pairs that changed. Means and counts compose
incrementally; medians and p90 do not, and approximating them with a sketch would
buy nothing against a scan that takes well under a second.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.config import AVERAGES_RULE_VERSION, PUNCTUAL_S
from dbrel.identity import SOURCE_GTFS, SOURCE_SYNTH, SOURCE_TT
from dbrel.models import (
    BASIS_REAL,
    BASIS_SYNTH,
    QUALITY_CANCELLED,
    TRUSTED_QUALITIES,
    Train,
    TrainStationDelay,
    Trip,
    TripStop,
)

# Which trip.source values feed which basis. gtfs is listed with the real sources
# because if that reader is ever rebuilt its rows are observations too; today it
# materialises no trips, so it contributes nothing.
SOURCES_BY_BASIS: dict[str, tuple[str, ...]] = {
    BASIS_REAL: (SOURCE_TT, SOURCE_GTFS),
    BASIS_SYNTH: (SOURCE_SYNTH,),
}

# One day of slack on the watermark, so a --force re-finalize that rewrites
# yesterday's grades is still picked up as dirty.
_WATERMARK_SLACK_S = 86_400


@dataclass(slots=True)
class DelayAveragesResult:
    basis: str = BASIS_REAL
    pairs_considered: int = 0
    pairs_written: int = 0
    pairs_deleted: int = 0
    samples_read: int = 0
    elapsed_s: float = 0.0


@dataclass(slots=True)
class _Samples:
    """Accumulator for one (station, train) pair."""

    arr: list[int]
    dep: list[int]
    trips: set[int]
    cancelled: int = 0
    known: int = 0
    first_date: str | None = None
    last_date: str | None = None

    def observe_date(self, service_date: str | None) -> None:
        if not service_date:
            return
        if self.first_date is None or service_date < self.first_date:
            self.first_date = service_date
        if self.last_date is None or service_date > self.last_date:
            self.last_date = service_date


def _percentile(ordered: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile over an already-sorted list.

    SQLite has no percentile_cont, and sorting each pair's samples in Python is
    exact, single-pass and needs no extension.
    """
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))
    return ordered[index]


def _rate(values: list[int], threshold: int) -> float | None:
    if not values:
        return None
    return sum(1 for v in values if v < threshold) / len(values)


def _dirty_pairs(session: Session, sources: tuple[str, ...], basis: str) -> set[tuple[int, int]]:
    """(station_id, train_id) pairs whose samples may have changed since last run.

    Keyed on trip_stop.last_observed_at, not trip.finalized_at. finalized_at
    governs *gap grading* and is deliberately withheld for six hours after a run
    ends, because a later read can still turn a NO_DATA into a value — but a
    RECORDED delay is already settled when it is written, since the poller only
    records an event once (ct or pt) + 120 s is in the past. Waiting on
    finalized_at would therefore delay every average by six hours and buy no
    accuracy at all: on a fresh database it would produce nothing whatsoever.
    """
    watermark = (
        session.scalar(
            select(func.max(TrainStationDelay.computed_at)).where(
                TrainStationDelay.basis == basis
            )
        )
        or 0
    )
    # Two columns with .distinct(), not distinct(tuple_(...)): most backends
    # cannot SELECT from a tuple construct, and SQLAlchemy refuses to compile it.
    rows = session.execute(
        select(TripStop.station_id, Trip.train_id)
        .join(Trip, Trip.id == TripStop.trip_id)
        .where(
            Trip.source.in_(sources),
            TripStop.last_observed_at.isnot(None),
            TripStop.last_observed_at >= watermark - _WATERMARK_SLACK_S,
        )
        .distinct()
    ).all()
    return {(station_id, train_id) for station_id, train_id in rows}


def _collect(
    session: Session,
    sources: tuple[str, ...],
    pairs: set[tuple[int, int]] | None,
    station_ids: list[int] | None,
    since: str | None,
) -> tuple[dict[tuple[int, int], _Samples], int]:
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
            Trip.source.in_(sources),
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
    samples: dict[tuple[int, int], _Samples] = defaultdict(
        lambda: _Samples(arr=[], dep=[], trips=set())
    )
    seen = 0
    stream = session.execute(query.execution_options(yield_per=10_000))
    for station_id, train_id, trip_id, service_date, arr, dep, quality in stream:
        if station_id is None or train_id is None:
            continue
        key = (station_id, train_id)
        if pairs is not None and key not in pairs:
            continue
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


def _attributes(session: Session, train_ids: Iterable[int]) -> dict[int, tuple[int | None, str | None]]:
    ids = list(train_ids)
    if not ids:
        return {}
    return {
        train_id: (line_id, segment)
        for train_id, line_id, segment in session.execute(
            select(Train.id, Train.line_id, Train.segment).where(Train.id.in_(ids))
        )
    }


def run_delay_averages(
    session: Session,
    *,
    basis: str = BASIS_REAL,
    since: str | None = None,
    full: bool = False,
    station_ids: list[int] | None = None,
    now: int | None = None,
    progress=lambda msg: None,
) -> DelayAveragesResult:
    started = time.monotonic()
    now = now or int(time.time())
    result = DelayAveragesResult(basis=basis)

    sources = SOURCES_BY_BASIS.get(basis)
    if sources is None:
        raise ValueError(f"unknown basis {basis!r}; expected one of {sorted(SOURCES_BY_BASIS)}")

    pairs = None if full else _dirty_pairs(session, sources, basis)
    if pairs is not None and not pairs:
        progress("  nothing to recompute")
        result.elapsed_s = time.monotonic() - started
        return result

    samples, result.samples_read = _collect(session, sources, pairs, station_ids, since)
    result.pairs_considered = len(pairs) if pairs is not None else len(samples)
    attributes = _attributes(session, {train_id for _, train_id in samples})

    rows = []
    for (station_id, train_id), bucket in samples.items():
        arr = sorted(bucket.arr)
        dep = sorted(bucket.dep)
        line_id, segment = attributes.get(train_id, (None, None))
        rows.append(
            {
                "station_id": station_id,
                "train_id": train_id,
                "basis": basis,
                "line_id": line_id,
                "segment": segment,
                "n_arr": len(arr),
                "n_dep": len(dep),
                "n_runs": len(bucket.trips),
                "n_cancelled": bucket.cancelled,
                "n_known": bucket.known,
                "avg_arr_delay_s": sum(arr) / len(arr) if arr else None,
                "avg_dep_delay_s": sum(dep) / len(dep) if dep else None,
                "p50_arr_delay_s": _percentile(arr, 0.5),
                "p50_dep_delay_s": _percentile(dep, 0.5),
                "p90_arr_delay_s": _percentile(arr, 0.9),
                "p90_dep_delay_s": _percentile(dep, 0.9),
                "max_dep_delay_s": dep[-1] if dep else None,
                "pct_arr_punctual": _rate(arr, PUNCTUAL_S),
                "pct_dep_punctual": _rate(dep, PUNCTUAL_S),
                "first_service_date": bucket.first_date,
                "last_service_date": bucket.last_date,
                "computed_at": now,
                "rule_version": AVERAGES_RULE_VERSION,
            }
        )

    if rows:
        statement = sqlite_insert(TrainStationDelay)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["station_id", "train_id", "basis"],
                set_={
                    column: statement.excluded[column]
                    for column in rows[0]
                    if column not in ("station_id", "train_id", "basis")
                },
            ),
            rows,
        )
        result.pairs_written = len(rows)

    # A pair with no qualifying samples left has to lose its row, or the board
    # keeps showing an average for a stop the train no longer makes.
    #
    # Incrementally we can only check pairs something told us to look at: a pair
    # whose samples have vanished entirely leaves no trace to mark it dirty, so
    # it survives until a --full pass, which reconciles against the stored rows.
    if pairs is None:
        stale_scope = {
            (station_id, train_id)
            for station_id, train_id in session.execute(
                select(TrainStationDelay.station_id, TrainStationDelay.train_id).where(
                    TrainStationDelay.basis == basis
                )
            )
        }
    else:
        stale_scope = pairs

    if stale_scope:
        empty = sorted(stale_scope - set(samples))
        for chunk in range(0, len(empty), 500):
            batch = empty[chunk : chunk + 500]
            session.execute(
                delete(TrainStationDelay).where(
                    TrainStationDelay.basis == basis,
                    or_(
                        *[
                            and_(
                                TrainStationDelay.station_id == station_id,
                                TrainStationDelay.train_id == train_id,
                            )
                            for station_id, train_id in batch
                        ]
                    ),
                )
            )
        result.pairs_deleted = len(empty)

    session.flush()
    result.elapsed_s = time.monotonic() - started
    return result
