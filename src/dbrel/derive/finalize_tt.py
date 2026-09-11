"""Derive actual times for Timetables runs.

The rule is unchanged from v1 — *the last published estimate for an event,
observed after the event should have happened, that then stopped changing* — and
if anything it fits IRIS better: `ct` is unlabelled, an estimate before passage
and DB's recorded actual afterwards, with the API never saying which. So the
same conservative framing, and the same audit trail, are still required.

Three things genuinely differ, and each has a comment where it happens:

1. **The listening record is per station.** Under GTFS-RT one fetch covered all
   of Germany, so one global list of poll times was sound. Here one fetch covers
   one station, and grading a Bochum stop on the strength of a Dortmund poll
   would quietly corrupt everything downstream.

2. **Cancellations are stated, not inferred.** cs='c' and clt are authoritative,
   so trip_stop.cancelled finally carries real values.

A third change was designed and then deleted: a PLAN_HELD grade for punctual
trains, on the assumption that /fchg published only changes and so never
mentioned them. It does mention them — 100% of planned events appear, punctual
ones as ct == pt (findings §18) — so on-time running arrives as an ordinary
observation and absence still means absence.

The GTFS path in finalize.py is deliberately untouched, so `finalize --source
gtfs` still reproduces rule v1 exactly. The grading primitives are imported from
there rather than copied.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from dbrel.config import GRACE_S, SETTLE_S
from dbrel.derive.finalize import (
    _dominant_quality,
    _grade_stop,
    _predicted,
    _resolve,
)
from dbrel.gtfs.timeutil import local_hour
from dbrel.models import (
    QUALITY_CANCELLED,
    QUALITY_NO_DATA,
    StationStop,
    TrackedStation,
    Trip,
    TripStop,
    TtObservation,
    TtPoll,
)

# rule_version 2: per-station listening record + explicit cancellation.
RULE_VERSION_TT = 2


@dataclass(slots=True)
class FinalizeTtResult:
    trips_considered: int = 0
    trips_finalized: int = 0
    stops_written: int = 0
    by_quality: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    cancelled_stops: int = 0
    elapsed_s: float = 0.0


@dataclass(slots=True)
class _Event:
    stop_sequence: int
    station_id: int
    station_stop_id: int
    supposed_arr: int | None
    supposed_dep: int | None


def _poll_times_by_station(session: Session) -> dict[int, list[int]]:
    """station_id -> ascending fetch times of successful polls.

    This is the whole of change (1): the listening record now has a station
    dimension, because a Timetables fetch is evidence about one station only.
    """
    rows = session.execute(
        select(TtPoll.station_id, TtPoll.fetched_at)
        .where(TtPoll.error.is_(None), TtPoll.http_status == 200)
        .order_by(TtPoll.fetched_at)
    ).all()
    out: dict[int, list[int]] = defaultdict(list)
    for station_id, fetched_at in rows:
        if station_id is not None:
            out[station_id].append(fetched_at)
    return out


def _settle_for(station_id: int, gaps: dict[int, int | None]) -> int:
    """A value must outlast two poll intervals to count as settled.

    At the pilot's 30 s cadence SETTLE_S dominates; at a 12-minute cadence it
    would not, and grading anything OBSERVED against a global 300 s would be
    wishful. Hence per-station rather than a constant.
    """
    gap = gaps.get(station_id)
    return max(SETTLE_S, 2 * gap) if gap else SETTLE_S


def run_finalize_tt(
    session: Session,
    *,
    service_date: str | None = None,
    force: bool = False,
    now: int | None = None,
    progress=lambda msg: None,
) -> FinalizeTtResult:
    started = time.monotonic()
    now = now or int(time.time())
    result = FinalizeTtResult()

    polls_by_station = _poll_times_by_station(session)
    gaps = {
        row[0]: row[1]
        for row in session.execute(
            select(TrackedStation.station_id, TrackedStation.median_poll_gap_s)
        ).all()
    }

    query = select(Trip).where(Trip.source == "tts", Trip.supposed_arrival_utc.isnot(None))
    if service_date:
        query = query.where(Trip.service_date == service_date)
    else:
        # Closed once our last TRACKED stop has passed by enough to settle —
        # which is correct, since nothing more can be learned about the run.
        query = query.where(Trip.supposed_arrival_utc < now - (GRACE_S + SETTLE_S))
    if not force:
        query = query.where(Trip.finalized_at.is_(None))

    trips = session.execute(query).scalars().all()
    result.trips_considered = len(trips)
    if not trips:
        result.elapsed_s = time.monotonic() - started
        return result

    trip_ids = [t.id for t in trips]
    train_ids = list({t.train_id for t in trips})

    # Planned pattern, keyed by (train, stop_sequence). Sequences are sparse.
    pattern: dict[int, dict[int, _Event]] = defaultdict(dict)
    for ss in (
        session.execute(select(StationStop).where(StationStop.train_id.in_(train_ids)))
        .scalars()
        .all()
    ):
        pattern[ss.train_id][ss.stop_sequence] = _Event(
            stop_sequence=ss.stop_sequence,
            station_id=ss.station_id,
            station_stop_id=ss.id,
            supposed_arr=None,
            supposed_dep=None,
        )

    # Supposed instants come from the trip's own service date, so a pattern
    # rebuild cannot retroactively change a historical fact.
    supposed_by_trip = _supposed_times(session, trips, pattern)

    observations: dict[int, dict[int, list[TtObservation]]] = defaultdict(lambda: defaultdict(list))
    chunk = 900
    for start in range(0, len(trip_ids), chunk):
        for obs in (
            session.execute(
                select(TtObservation)
                .where(TtObservation.trip_id.in_(trip_ids[start : start + chunk]))
                .order_by(TtObservation.observed_at)
            )
            .scalars()
            .all()
        ):
            observations[obs.trip_id][obs.stop_sequence].append(obs)

    all_rows: list[dict] = []
    for trip in trips:
        events = [
            _Event(
                stop_sequence=seq,
                station_id=ev.station_id,
                station_stop_id=ev.station_stop_id,
                supposed_arr=supposed_by_trip.get((trip.id, seq), (None, None))[0],
                supposed_dep=supposed_by_trip.get((trip.id, seq), (None, None))[1],
            )
            for seq, ev in sorted(pattern.get(trip.train_id, {}).items())
        ]
        if not events:
            continue
        rows, rollup = _grade_trip_tt(
            trip, events, observations.get(trip.id, {}), polls_by_station, gaps, now
        )
        all_rows.extend(rows)
        for key, value in rollup.items():
            setattr(trip, key, value)
        trip.rule_version = RULE_VERSION_TT
        trip.finalized_at = now
        result.trips_finalized += 1
        for row in rows:
            result.by_quality[row["quality"]] += 1
            result.cancelled_stops += row["cancelled"]

    if all_rows:
        if force:
            session.execute(delete(TripStop).where(TripStop.trip_id.in_(trip_ids)))
        session.execute(insert(TripStop), all_rows)
        result.stops_written = len(all_rows)

    session.flush()
    result.elapsed_s = time.monotonic() - started
    return result


def _supposed_times(session, trips, pattern) -> dict[tuple[int, int], tuple[int | None, int | None]]:
    """(trip_id, stop_sequence) -> (supposed_arr, supposed_dep) as absolute instants."""
    from datetime import date as date_cls
    from datetime import datetime

    from dbrel.config import TZ

    out: dict[tuple[int, int], tuple[int | None, int | None]] = {}
    rows = session.execute(
        select(
            StationStop.train_id,
            StationStop.stop_sequence,
            StationStop.planned_arrival_sec,
            StationStop.planned_departure_sec,
        ).where(StationStop.train_id.in_(list({t.train_id for t in trips})))
    ).all()
    by_train: dict[int, list[tuple]] = defaultdict(list)
    for train_id, seq, arr, dep in rows:
        by_train[train_id].append((seq, arr, dep))

    for trip in trips:
        day = date_cls.fromisoformat(trip.service_date)
        anchor = datetime(day.year, day.month, day.day, 12, tzinfo=TZ).timestamp() - 12 * 3600
        for seq, arr, dep in by_train.get(trip.train_id, ()):
            out[(trip.id, seq)] = (
                int(anchor + arr) if arr is not None else None,
                int(anchor + dep) if dep is not None else None,
            )
    return out


def _grade_trip_tt(trip, events, trip_obs, polls_by_station, gaps, now):
    rows: list[dict] = []
    delays: list[int] = []
    n_observed = 0
    n_cancelled = 0

    trip_seen_after_passage = False
    for event in events:
        obs = trip_obs.get(event.stop_sequence, [])
        reference = event.supposed_dep or event.supposed_arr
        if not obs or reference is None:
            continue
        predicted = _predicted(obs[-1], event) or reference
        if obs[-1].observed_at >= predicted + GRACE_S:
            trip_seen_after_passage = True
            break

    for event in events:
        obs = trip_obs.get(event.stop_sequence, [])
        station_polls = polls_by_station.get(event.station_id, [])
        settle = _settle_for(event.station_id, gaps)
        supposed_arr, supposed_dep = event.supposed_arr, event.supposed_dep
        reference = supposed_dep or supposed_arr

        actual_arr = actual_dep = arr_delay = dep_delay = poll_gap = None
        cancelled = arr_cancelled = dep_cancelled = 0
        cancelled_at = None
        platform_planned = platform_changed = None

        if obs:
            last = obs[-1]
            platform_planned = last.arr_platform or last.dep_platform
            platform_changed = last.dep_platform or last.arr_platform

        if obs and (obs[-1].arr_status == "c" or obs[-1].dep_status == "c"):
            # (3) Stated outright by IRIS, so no inference and no delay contribution.
            last = obs[-1]
            quality = QUALITY_CANCELLED
            cancelled = 1
            arr_cancelled = int(last.arr_status == "c")
            dep_cancelled = int(last.dep_status == "c")
            cancelled_at = last.cancelled_at_utc
            n_cancelled += 1
        elif not obs:
            # Silence means out of horizon or unobserved, never "on time":
            # punctual trains are published explicitly (findings §18).
            quality = QUALITY_NO_DATA
        else:
            last = obs[-1]
            actual_arr, arr_delay = _resolve(last.arr_time_utc, last.arr_delay_s, supposed_arr)
            actual_dep, dep_delay = _resolve(last.dep_time_utc, last.dep_delay_s, supposed_dep)
            predicted = _predicted(last, event) or reference
            # (1) station_polls, not a global list.
            quality, poll_gap = _grade_stop(
                last_observed_at=last.observed_at,
                predicted=predicted,
                polls=station_polls,
                trip_seen_after_passage=trip_seen_after_passage,
                last_seen=None,
                settle_s=settle,
            )
            n_observed += 1
            if dep_delay is not None:
                delays.append(dep_delay)
            elif arr_delay is not None:
                delays.append(arr_delay)

        rows.append(
            {
                "trip_id": trip.id,
                "stop_sequence": event.stop_sequence,
                "station_id": event.station_id,
                "station_stop_id": event.station_stop_id,
                "supposed_arrival_utc": supposed_arr,
                "supposed_departure_utc": supposed_dep,
                "actual_arrival_utc": actual_arr,
                "actual_departure_utc": actual_dep,
                "arr_delay_s": arr_delay,
                "dep_delay_s": dep_delay,
                "quality": quality,
                "rule_version": RULE_VERSION_TT,
                "n_observations": len(obs),
                "last_observed_at": obs[-1].observed_at if obs else None,
                "poll_gap_s": poll_gap,
                "cancelled": cancelled,
                "skipped": 0,
                "arr_cancelled": arr_cancelled,
                "dep_cancelled": dep_cancelled,
                "cancelled_at_utc": cancelled_at,
                "added": 0,
                "planned_platform": platform_planned,
                "changed_platform": platform_changed,
                "is_handover": 0,
                "supposed_departure_hour": local_hour(supposed_dep) if supposed_dep else None,
            }
        )

    first = rows[0] if rows else {}
    last_row = rows[-1] if rows else {}
    all_cancelled = bool(rows) and n_cancelled == len(rows)
    status = "CANCELLED" if all_cancelled else ("COMPLETED" if n_observed else "MISSING")
    return rows, {
        "actual_departure_utc": first.get("actual_departure_utc"),
        "actual_arrival_utc": last_row.get("actual_arrival_utc"),
        "final_delay_s": last_row.get("arr_delay_s") or last_row.get("dep_delay_s"),
        "max_delay_s": max(delays) if delays else None,
        "n_stops_observed": n_observed,
        "status": status,
        "quality": _dominant_quality(rows),
        "cancelled_at_utc": next((r["cancelled_at_utc"] for r in rows if r["cancelled_at_utc"]), None),
    }
