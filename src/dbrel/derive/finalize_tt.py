"""Derive actual times for Timetables runs.

v1 had to infer an actual, because GTFS-RT publishes forecasts and a value that
stops changing leaves no trace: "settled" was reconstructed from silence plus
evidence that we were still listening. The archival reader removes that problem
at the source. changes_poller only records an event once it has already happened,
so a stored value is DB's recorded time rather than an estimate, and the grade is
QUALITY_RECORDED — asserted directly, with read_lag_s saying how long after the
event we read it.

What still needs care is the *absence* of an observation, and here the per-station
listening record earns its keep. A Timetables fetch is evidence about one station
only, so grading a Bochum stop on the strength of a Dortmund poll would quietly
corrupt everything downstream. With it we can separate:

* NO_DATA — we were polling this station across the window and nothing appeared.
  A fact about the railway. Never means "on time": punctual running is published
  explicitly as ct == pt (findings §18).
* PREDICTION_ONLY — no poll covered the window at all. A blind spot in our own
  coverage, with poll_gap_s recording how wide it was.

Cancellations are stated rather than inferred (cs='c' plus clt), so they bypass
grading entirely.

The GTFS path in finalize.py is deliberately untouched, so rule v1 still
reproduces exactly. Its primitives are imported rather than copied.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.config import GRACE_S, SERVICE_DATE_CLOSE_S, SETTLE_S, TT_LINGER_S
from dbrel.derive.finalize import (
    _dominant_quality,
    _polled_between,
    _resolve,
)
from dbrel.gtfs.timeutil import local_hour
from dbrel.models import (
    QUALITY_CANCELLED,
    QUALITY_NO_DATA,
    QUALITY_PREDICTION_ONLY,
    QUALITY_RECORDED,
    StationStop,
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
    # pp, from the schedule. Observations carry only cp, so the planned platform
    # has to come from here — coalescing the two upstream made them indistinguishable.
    planned_platform: str | None = None


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

    # A run whose only tracked stop is its origin has no tracked arrival, and
    # requiring one excluded 22% of runs from finalization permanently.
    last_event = func.coalesce(Trip.supposed_arrival_utc, Trip.supposed_departure_utc)

    query = select(Trip).where(Trip.source == "tts", last_event.isnot(None))
    if service_date:
        query = query.where(Trip.service_date == service_date)
    else:
        query = query.where(last_event < now - (GRACE_S + SETTLE_S))
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
            planned_platform=ss.platform_code,
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
                planned_platform=ev.planned_platform,
            )
            for seq, ev in sorted(pattern.get(trip.train_id, {}).items())
        ]
        if not events:
            continue
        rows, rollup = _grade_trip_tt(
            trip, events, observations.get(trip.id, {}), polls_by_station, now
        )
        all_rows.extend(rows)
        for key, value in rollup.items():
            setattr(trip, key, value)
        trip.rule_version = RULE_VERSION_TT
        # Stamp finalized_at only once no later poll could change the answer.
        # A passed event lingers in the /fchg horizon for at least 75 minutes
        # (findings §18 — a floor bounded by the pilot's watch, not the API), so a
        # run graded minutes after its last stop may still be waiting for values a
        # later read would supply. Stamping now would freeze those NO_DATA rows in
        # place until someone thought to pass --force.
        if (trip.supposed_arrival_utc or trip.supposed_departure_utc or 0) < (
            now - SERVICE_DATE_CLOSE_S
        ):
            trip.finalized_at = now
        result.trips_finalized += 1
        for row in rows:
            result.by_quality[row["quality"]] += 1
            result.cancelled_stops += row["cancelled"]

    if force:
        # Outside the all_rows guard on purpose: a forced re-run that now yields
        # nothing must still clear what the previous rule wrote.
        session.execute(delete(TripStop).where(TripStop.trip_id.in_(trip_ids)))
    if all_rows:
        # Upsert, because a run is re-graded on every pass until it is closed —
        # each read can add stops the previous one had no value for.
        stmt = sqlite_insert(TripStop)
        stmt = stmt.on_conflict_do_update(
            index_elements=["trip_id", "stop_sequence"],
            set_={
                column: getattr(stmt.excluded, column)
                for column in all_rows[0]
                if column not in ("trip_id", "stop_sequence")
            },
        )
        session.execute(stmt, all_rows)
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


def _grade_trip_tt(trip, events, trip_obs, polls_by_station, now):
    rows: list[dict] = []
    delays: list[int] = []
    n_observed = 0
    n_cancelled = 0

    for event in events:
        obs = trip_obs.get(event.stop_sequence, [])
        station_polls = polls_by_station.get(event.station_id, [])
        supposed_arr, supposed_dep = event.supposed_arr, event.supposed_dep
        reference = supposed_dep or supposed_arr

        actual_arr = actual_dep = arr_delay = dep_delay = None
        poll_gap = read_lag = None
        cancelled = arr_cancelled = dep_cancelled = 0
        cancelled_at = None
        platform_changed = None
        last = obs[-1] if obs else None

        if last is not None:
            # cp only. pp comes from the schedule via event.planned_platform.
            platform_changed = last.dep_platform or last.arr_platform

        if last is not None and (last.arr_status == "c" or last.dep_status == "c"):
            # Stated outright by IRIS, so no inference and no delay contribution.
            quality = QUALITY_CANCELLED
            cancelled = 1
            arr_cancelled = int(last.arr_status == "c")
            dep_cancelled = int(last.dep_status == "c")
            cancelled_at = last.cancelled_at_utc
            n_cancelled += 1
        elif last is not None:
            actual_arr, arr_delay = _resolve(last.arr_time_utc, last.arr_delay_s, supposed_arr)
            actual_dep, dep_delay = _resolve(last.dep_time_utc, last.dep_delay_s, supposed_dep)
            # The poller stores an event only once it has already happened, so a
            # stored value is a recorded time, not a forecast. Nothing to infer.
            quality = QUALITY_RECORDED
            happened = actual_dep or actual_arr
            if happened is not None:
                read_lag = last.observed_at - happened
            n_observed += 1
            if dep_delay is not None:
                delays.append(dep_delay)
            elif arr_delay is not None:
                delays.append(arr_delay)
        elif reference is not None and _polled_between(
            station_polls, reference + GRACE_S, reference + TT_LINGER_S
        ):
            # We were reading this station while the value would still have been in
            # its horizon, and nothing came. A fact about the railway — and never
            # "on time", since punctual running is published explicitly (§18).
            quality = QUALITY_NO_DATA
        else:
            # Our own blind spot, not the railway's silence.
            quality = QUALITY_PREDICTION_ONLY
            if reference is not None:
                latest = station_polls[-1] if station_polls else None
                poll_gap = max(0, reference + TT_LINGER_S - latest) if latest else TT_LINGER_S

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
                "last_observed_at": last.observed_at if last is not None else None,
                "poll_gap_s": poll_gap,
                "read_lag_s": read_lag,
                "cancelled": cancelled,
                "skipped": 0,
                "arr_cancelled": arr_cancelled,
                "dep_cancelled": dep_cancelled,
                "cancelled_at_utc": cancelled_at,
                "added": 0,
                "planned_platform": event.planned_platform,
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
