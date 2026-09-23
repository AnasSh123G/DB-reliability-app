"""Account for the stops that produced no recorded value, and roll runs up.

The poller writes every stop it has an actual time for, straight into trip_stop.
That leaves this pass two jobs, both about absence:

* Fill the gaps. A scheduled stop with no value is still a fact, and which fact it
  is depends entirely on whether we were listening:

  - NO_DATA — we were polling this station across the window in which the value
    would still have been in /fchg's horizon, and nothing appeared. A fact about
    the railway. Never means "on time": punctual running is published explicitly
    as ct == pt (docs/probe-findings.md §18).
  - PREDICTION_ONLY — no poll covered the window at all. A blind spot in our own
    coverage, with poll_gap_s recording how wide it was.

  This is where the per-station listening record earns its keep. A Timetables fetch
  is evidence about one station only, so grading a Bochum stop on the strength of a
  Dortmund poll would quietly corrupt everything downstream.

* Roll each run up from its stops: final and maximum delay, how many stops were
  actually recorded, and whether the run completed, was cancelled, or never showed.

Gap rows are inserted ON CONFLICT DO NOTHING, so this pass can never overwrite or
downgrade a value the poller recorded.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.config import GRACE_S, SERVICE_DATE_CLOSE_S, SETTLE_S, TT_LINGER_S
from dbrel.ingest.timeutil import local_hour
from dbrel.models import (
    QUALITY_NO_DATA,
    QUALITY_PREDICTION_ONLY,
    QUALITY_RECORDED,
    StationStop,
    Trip,
    TripStop,
    TtPoll,
)
from dbrel.ingest.runs import epoch_of_sec

# rule_version 2: per-station listening record + explicit cancellation.
RULE_VERSION_TT = 2

# Grades this pass is allowed to write, and therefore to replace on a re-run.
GAP_QUALITIES = (QUALITY_NO_DATA, QUALITY_PREDICTION_ONLY)


@dataclass(slots=True)
class FinalizeTtResult:
    trips_considered: int = 0
    trips_finalized: int = 0
    gaps_written: int = 0
    by_quality: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    cancelled_stops: int = 0
    elapsed_s: float = 0.0


def _polled_between(polls: list[int], start: int, end: int) -> bool:
    """Was there a successful poll of this station while the value was readable?

    The difference between "the railway published nothing" and "we were not
    looking". Only the first is a fact about the railway; recording the second as
    though it were would poison every statistic derived from the table.
    """
    if not polls or start > end:
        return False
    index = bisect_left(polls, start)
    return index < len(polls) and polls[index] <= end


def _poll_times_by_station(session: Session) -> dict[int, list[int]]:
    """station_id -> ascending fetch times of successful polls."""
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


def _dominant_quality(rows: list[tuple]) -> str | None:
    """The grade most of a run's stops carry. rows are (quality, ...)."""
    if not rows:
        return None
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row[0]] += 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


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

    if force:
        # Only this pass's own rows. Deleting the recorded ones would throw away
        # values that cannot be fetched again once they fall out of the horizon.
        for start in range(0, len(trip_ids), 900):
            session.execute(
                delete(TripStop).where(
                    TripStop.trip_id.in_(trip_ids[start : start + 900]),
                    TripStop.quality.in_(GAP_QUALITIES),
                )
            )

    # The planned pattern, as absolute instants for each run's own service date, so
    # a later feed rebuild cannot retroactively change a historical fact.
    #
    # Keyed by the trip's OWN path_sha1 (see StationStop.path_sha1 / runs.py
    # upsert_run): a numbered TT train can hold more than one station_stop
    # variant (a wing/coupled service some days, solo others, or even
    # incidental digest drift from which tracked station happened to be the
    # earliest one seen that day), each with its own, non-interchangeable
    # stop_sequence numbering. Grouping every variant's rows under one
    # (train_id, service_date) key -- as this used to, and as the identical
    # helper in changes_poller._load_patterns used to before its own fix --
    # let a later variant's row for some OTHER station silently overwrite an
    # earlier variant's row for THIS trip's own stop_sequence, so a gap got
    # filled in with a station this trip never actually called at.
    dates_by_train: dict[int, dict[str, str | None]] = defaultdict(dict)
    for trip in trips:
        dates_by_train[trip.train_id][trip.service_date] = trip.path_sha1
    pattern = _load_patterns(session, list(dates_by_train), dates_by_train)

    recorded = _recorded_sequences(session, trip_ids)

    gap_rows: list[dict] = []
    for trip in trips:
        stops = pattern.get((trip.train_id, trip.service_date), {})
        have = recorded.get(trip.id, set())
        for seq, planned in stops.items():
            if seq in have:
                continue
            reference = planned.supposed_dep or planned.supposed_arr
            station_polls = polls_by_station.get(planned.station_id, [])
            poll_gap = None
            if reference is not None and _polled_between(
                station_polls, reference + GRACE_S, reference + TT_LINGER_S
            ):
                quality = QUALITY_NO_DATA
            else:
                quality = QUALITY_PREDICTION_ONLY
                if reference is not None:
                    latest = station_polls[-1] if station_polls else None
                    poll_gap = (
                        max(0, reference + TT_LINGER_S - latest) if latest else TT_LINGER_S
                    )
            gap_rows.append(
                {
                    "trip_id": trip.id,
                    "stop_sequence": seq,
                    "station_id": planned.station_id,
                    "station_stop_id": planned.station_stop_id,
                    "supposed_arrival_utc": planned.supposed_arr,
                    "supposed_departure_utc": planned.supposed_dep,
                    "actual_arrival_utc": None,
                    "actual_departure_utc": None,
                    "arr_delay_s": None,
                    "dep_delay_s": None,
                    "quality": quality,
                    "rule_version": RULE_VERSION_TT,
                    "last_observed_at": None,
                    "poll_gap_s": poll_gap,
                    "read_lag_s": None,
                    "cancelled": 0,
                    "arr_cancelled": 0,
                    "dep_cancelled": 0,
                    "cancelled_at_utc": None,
                    "added": 0,
                    "planned_platform": planned.platform_code,
                    "changed_platform": None,
                    "is_handover": 0,
                    "supposed_departure_hour": local_hour(planned.supposed_dep)
                    if planned.supposed_dep
                    else None,
                }
            )

    if gap_rows:
        # DO NOTHING, never DO UPDATE: a recorded value outranks anything this pass
        # could say about the same stop.
        session.execute(
            sqlite_insert(TripStop).on_conflict_do_nothing(
                index_elements=["trip_id", "stop_sequence"]
            ),
            gap_rows,
        )
        result.gaps_written = len(gap_rows)
    session.flush()

    # --- roll each run up from whatever its stops now say --------------------
    by_trip = _rollup_source(session, trip_ids)
    for trip in trips:
        rows = by_trip.get(trip.id, [])
        if not rows:
            continue
        for key, value in _rollup(rows).items():
            setattr(trip, key, value)
        trip.rule_version = RULE_VERSION_TT
        # Stamp finalized_at only once no later poll could change the answer. A
        # passed event lingers in the /fchg horizon for about 15 hours, so a run
        # closed minutes after its last stop may still be waiting for values a
        # later read would supply; stamping now would freeze those gap rows in
        # place until someone thought to pass --force.
        if (trip.supposed_arrival_utc or trip.supposed_departure_utc or 0) < (
            now - SERVICE_DATE_CLOSE_S
        ):
            trip.finalized_at = now
        result.trips_finalized += 1
        for row in rows:
            result.by_quality[row[0]] += 1
            result.cancelled_stops += row[6]

    session.flush()
    result.elapsed_s = time.monotonic() - started
    return result


@dataclass(slots=True)
class _Planned:
    station_stop_id: int
    station_id: int
    platform_code: str | None
    supposed_arr: int | None
    supposed_dep: int | None


def _load_patterns(
    session: Session, train_ids: list[int], dates_by_train: dict[int, dict[str, str | None]]
) -> dict[tuple[int, str], dict[int, _Planned]]:
    """(train_id, service_date) -> stop_sequence -> the planned stop.

    `dates_by_train` maps each wanted service_date to that trip's own
    path_sha1; a station_stop row is only offered to a service_date whose
    trip was built from the SAME variant (`None == None` still matches, for
    pre-migration/GTFS rows with no digest recorded either side). See the
    caller's comment.
    """
    out: dict[tuple[int, str], dict[int, _Planned]] = {}
    for start in range(0, len(train_ids), 900):
        rows = session.execute(
            select(
                StationStop.id,
                StationStop.train_id,
                StationStop.path_sha1,
                StationStop.stop_sequence,
                StationStop.station_id,
                StationStop.platform_code,
                StationStop.planned_arrival_sec,
                StationStop.planned_departure_sec,
            ).where(StationStop.train_id.in_(train_ids[start : start + 900]))
        ).all()
        for ss_id, train_id, ss_path_sha1, seq, station_id, platform, arr_sec, dep_sec in rows:
            for service_date, wanted_path_sha1 in dates_by_train.get(train_id, {}).items():
                if ss_path_sha1 != wanted_path_sha1:
                    continue
                out.setdefault((train_id, service_date), {})[seq] = _Planned(
                    station_stop_id=ss_id,
                    station_id=station_id,
                    platform_code=platform,
                    supposed_arr=epoch_of_sec(arr_sec, service_date),
                    supposed_dep=epoch_of_sec(dep_sec, service_date),
                )
    return out


def _recorded_sequences(session: Session, trip_ids: list[int]) -> dict[int, set[int]]:
    """trip_id -> the stop sequences that already have a row."""
    out: dict[int, set[int]] = defaultdict(set)
    for start in range(0, len(trip_ids), 900):
        rows = session.execute(
            select(TripStop.trip_id, TripStop.stop_sequence).where(
                TripStop.trip_id.in_(trip_ids[start : start + 900])
            )
        ).all()
        for trip_id, seq in rows:
            out[trip_id].add(seq)
    return out


def _rollup_source(session: Session, trip_ids: list[int]) -> dict[int, list[tuple]]:
    """trip_id -> its stops in sequence order, as the tuple _rollup expects."""
    out: dict[int, list[tuple]] = defaultdict(list)
    for start in range(0, len(trip_ids), 900):
        rows = session.execute(
            select(
                TripStop.quality,
                TripStop.actual_arrival_utc,
                TripStop.actual_departure_utc,
                TripStop.arr_delay_s,
                TripStop.dep_delay_s,
                TripStop.cancelled_at_utc,
                TripStop.cancelled,
                TripStop.trip_id,
            )
            .where(TripStop.trip_id.in_(trip_ids[start : start + 900]))
            .order_by(TripStop.trip_id, TripStop.stop_sequence)
        ).all()
        for row in rows:
            out[row[7]].append(row)
    return out


def _rollup(rows: list[tuple]) -> dict:
    """Journey-level summary of one run's stops."""
    delays = [row[4] if row[4] is not None else row[3] for row in rows]
    delays = [d for d in delays if d is not None]
    n_observed = sum(1 for row in rows if row[0] == QUALITY_RECORDED)
    n_cancelled = sum(row[6] for row in rows)
    first, last = rows[0], rows[-1]
    all_cancelled = n_cancelled == len(rows)
    return {
        "actual_departure_utc": first[2],
        "actual_arrival_utc": last[1],
        "final_delay_s": last[3] if last[3] is not None else last[4],
        "max_delay_s": max(delays) if delays else None,
        "n_stops_observed": n_observed,
        "status": "CANCELLED" if all_cancelled else ("COMPLETED" if n_observed else "MISSING"),
        "quality": _dominant_quality(rows),
        "cancelled_at_utc": next((row[5] for row in rows if row[5]), None),
    }
