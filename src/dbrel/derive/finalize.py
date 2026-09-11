"""Turn published predictions into defensible actual times.

The feed never states that a train departed. It publishes an estimate, revises it,
and eventually stops revising it. An "actual" here is therefore a derivation:

    the last published estimate for an event, observed after the event should have
    happened, that then stopped changing.

Because observations are stored only when a value *changes*, a value that stops
changing leaves no trace -- so "settled" is inferred from the absence of later
observations *combined with* evidence that we were still polling. That is what
rt_poll is for: without it, "on time" and "nobody was watching" look identical.

Every row records which rule produced it (rule_version) and how much confidence it
deserves (quality), so a corrected rule can be re-run over rt_observation and the
two results compared.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from dbrel.config import GRACE_S, RULE_VERSION, SETTLE_S
from dbrel.gtfs.timeutil import gtfs_sec_to_epoch, local_hour
from dbrel.models import (
    QUALITY_INFERRED_DOWNSTREAM,
    QUALITY_LAST_SEEN,
    QUALITY_MISSING,
    QUALITY_NO_DATA,
    QUALITY_OBSERVED,
    QUALITY_PREDICTION_ONLY,
    QUALITY_SKIPPED,
    RtObservation,
    RtPoll,
    StationStop,
    Trip,
    TripStop,
)
from dbrel.realtime.parser import STU_NO_DATA, STU_SKIPPED


@dataclass
class FinalizeResult:
    trips_considered: int = 0
    trips_finalized: int = 0
    trips_missing: int = 0
    trips_unmonitored: int = 0
    trips_skipped_open: int = 0
    stops_written: int = 0
    quality: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    elapsed_s: float = 0.0

    def summary(self) -> str:
        lines = [
            f"\nfinalized {self.trips_finalized:,} runs "
            f"({self.stops_written:,} stops) in {self.elapsed_s:.1f}s",
            f"  ran while we were polling but never appeared (MISSING): {self.trips_missing:,}",
            f"  no poll covered their window, left unjudged: {self.trips_unmonitored:,}",
            f"  still open, left for a later run: {self.trips_skipped_open:,}",
        ]
        if self.quality:
            lines.append("  stop quality:")
            total = sum(self.quality.values())
            for grade, count in sorted(self.quality.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {grade:22s} {count:>9,}  {count / total:6.1%}")
        return "\n".join(lines)


@dataclass(slots=True)
class _Event:
    """One stop of one run, as the rule sees it."""

    stop_sequence: int
    station_id: int
    station_stop_id: int
    supposed_arr: int | None
    supposed_dep: int | None


def _poll_times(session: Session) -> list[int]:
    """Fetch times of every successful poll, ascending. The listening record."""
    return [
        row[0]
        for row in session.execute(
            select(RtPoll.fetched_at).where(RtPoll.error.is_(None)).order_by(RtPoll.fetched_at)
        )
    ]


def _polled_at_or_after(polls: list[int], instant: int) -> bool:
    """Was there a successful poll at or after `instant`?"""
    if not polls:
        return False
    return polls[-1] >= instant


def _polled_between(polls: list[int], start: int, end: int) -> bool:
    """Was there a successful poll while this run was under way?

    This is the difference between "the train did not run" and "we were not looking".
    Only the first is a fact about the railway; recording the second as MISSING would
    poison every statistic derived from the table.
    """
    if not polls or start > end:
        return False
    index = bisect_left(polls, start)
    return index < len(polls) and polls[index] <= end


def run_finalize(
    session: Session,
    *,
    service_date: str | None = None,
    force: bool = False,
    now: int | None = None,
    progress=lambda msg: None,
) -> FinalizeResult:
    result = FinalizeResult()
    started = time.monotonic()
    now = now or int(time.time())
    polls = _poll_times(session)

    # A run is ready once its last scheduled arrival has passed by enough that a
    # settled value could have been observed.
    closed_before = now - (GRACE_S + SETTLE_S)

    query = select(Trip).where(Trip.supposed_arrival_utc.isnot(None))
    if service_date:
        query = query.where(Trip.service_date == service_date)
    else:
        query = query.where(Trip.supposed_arrival_utc < closed_before)
    if not force:
        query = query.where(Trip.finalized_at.is_(None))

    # Only runs we have some evidence about are worth materialising stops for; the
    # rest are recorded as MISSING at trip level. Doing otherwise would mean writing
    # ~1.5M stop rows per day for trains nobody observed.
    observed_trip_ids = {
        row[0] for row in session.execute(select(RtObservation.trip_id).distinct())
    }

    trips = session.execute(query).scalars().all()
    result.trips_considered = len(trips)
    progress(f"  {len(trips):,} runs to consider")

    ready: list[Trip] = []
    for trip in trips:
        if service_date and trip.supposed_arrival_utc >= closed_before:
            result.trips_skipped_open += 1
            continue
        if trip.id not in observed_trip_ids:
            window_start = (trip.supposed_departure_utc or trip.supposed_arrival_utc) - GRACE_S
            window_end = trip.supposed_arrival_utc + GRACE_S + SETTLE_S
            if not _polled_between(polls, window_start, window_end):
                # We were not watching. Leave the run untouched rather than assert
                # anything about it; a later poll history cannot retroactively cover it.
                result.trips_unmonitored += 1
                continue
            trip.status = QUALITY_MISSING
            trip.quality = QUALITY_MISSING
            trip.rule_version = RULE_VERSION
            trip.finalized_at = now
            trip.n_stops_observed = 0
            result.trips_missing += 1
            continue
        ready.append(trip)

    progress(f"  {len(ready):,} runs have observations")
    if not ready:
        result.elapsed_s = time.monotonic() - started
        return result

    for start in range(0, len(ready), 500):
        batch = ready[start : start + 500]
        _finalize_batch(session, batch, polls, now, result, force)
        session.flush()
        progress(f"  finalized {min(start + len(batch), len(ready)):,}/{len(ready):,}")

    result.elapsed_s = time.monotonic() - started
    return result


def _finalize_batch(
    session: Session,
    trips: list[Trip],
    polls: list[int],
    now: int,
    result: FinalizeResult,
    force: bool,
) -> None:
    trip_ids = [t.id for t in trips]

    if force:
        session.execute(delete(TripStop).where(TripStop.trip_id.in_(trip_ids)))

    # Planned pattern, keyed by (train, feed version) so a run keeps the timetable
    # it was actually scheduled under.
    keys = {(t.train_id, t.feed_version_id) for t in trips}
    pattern: dict[tuple[int, int], list[StationStop]] = defaultdict(list)
    for train_ids, fv_ids in [(sorted({k[0] for k in keys}), sorted({k[1] for k in keys}))]:
        for chunk_start in range(0, len(train_ids), 500):
            chunk = train_ids[chunk_start : chunk_start + 500]
            rows = session.execute(
                select(StationStop)
                .where(StationStop.train_id.in_(chunk), StationStop.feed_version_id.in_(fv_ids))
                .order_by(StationStop.train_id, StationStop.stop_sequence)
            ).scalars()
            for ss in rows:
                pattern[(ss.train_id, ss.feed_version_id)].append(ss)

    # Observations for these runs, grouped by (trip, stop), ascending in time.
    observations: dict[int, dict[int, list[RtObservation]]] = defaultdict(lambda: defaultdict(list))
    for chunk_start in range(0, len(trip_ids), 500):
        chunk = trip_ids[chunk_start : chunk_start + 500]
        rows = session.execute(
            select(RtObservation)
            .where(RtObservation.trip_id.in_(chunk))
            .order_by(RtObservation.trip_id, RtObservation.stop_sequence, RtObservation.observed_at)
        ).scalars()
        for obs in rows:
            observations[obs.trip_id][obs.stop_sequence].append(obs)

    stop_rows: list[dict] = []
    for trip in trips:
        stops = pattern.get((trip.train_id, trip.feed_version_id), [])
        if not stops:
            continue
        day = date.fromisoformat(trip.service_date)
        trip_obs = observations.get(trip.id, {})

        # The latest moment this run was seen at all, used to separate
        # "stopped updating because it settled" from "dropped out of the feed".
        last_seen = max(
            (o.observed_at for by_stop in trip_obs.values() for o in by_stop), default=None
        )

        events = [
            _Event(
                stop_sequence=ss.stop_sequence,
                station_id=ss.station_id,
                station_stop_id=ss.id,
                supposed_arr=(
                    gtfs_sec_to_epoch(day, ss.planned_arrival_sec)
                    if ss.planned_arrival_sec is not None
                    else None
                ),
                supposed_dep=(
                    gtfs_sec_to_epoch(day, ss.planned_departure_sec)
                    if ss.planned_departure_sec is not None
                    else None
                ),
            )
            for ss in stops
        ]

        rows, rollup = _grade_trip(trip, events, trip_obs, polls, last_seen, now)
        stop_rows.extend(rows)
        for row in rows:
            result.quality[row["quality"]] += 1

        trip.actual_departure_utc = rollup["actual_departure_utc"]
        trip.actual_arrival_utc = rollup["actual_arrival_utc"]
        trip.final_delay_s = rollup["final_delay_s"]
        trip.max_delay_s = rollup["max_delay_s"]
        trip.n_stops_observed = rollup["n_stops_observed"]
        trip.status = rollup["status"]
        trip.quality = rollup["quality"]
        trip.rule_version = RULE_VERSION
        trip.finalized_at = now
        result.trips_finalized += 1

    for chunk_start in range(0, len(stop_rows), 5000):
        session.execute(insert(TripStop), stop_rows[chunk_start : chunk_start + 5000])
    result.stops_written += len(stop_rows)


def _grade_trip(
    trip: Trip,
    events: list[_Event],
    trip_obs: dict[int, list[RtObservation]],
    polls: list[int],
    last_seen: int | None,
    now: int,
) -> tuple[list[dict], dict]:
    """Apply rule v1 to every stop of one run."""
    rows: list[dict] = []
    delays: list[int] = []
    n_observed = 0
    cancelled_stops = 0

    # Did any stop of this run get an update at or after its own passage time? If so,
    # a stop with no late update of its own was simply stable, not abandoned.
    trip_seen_after_passage = False
    for event in events:
        obs = trip_obs.get(event.stop_sequence, [])
        if not obs:
            continue
        reference = event.supposed_dep or event.supposed_arr
        if reference is None:
            continue
        predicted = _predicted(obs[-1], event) or reference
        if obs[-1].observed_at >= predicted + GRACE_S:
            trip_seen_after_passage = True
            break

    for event in events:
        obs = trip_obs.get(event.stop_sequence, [])
        supposed_dep = event.supposed_dep
        supposed_arr = event.supposed_arr
        reference = supposed_dep or supposed_arr

        actual_arr = actual_dep = None
        arr_delay = dep_delay = None
        poll_gap = None
        skipped = 0

        if not obs:
            quality = QUALITY_NO_DATA
        else:
            last = obs[-1]
            if last.schedule_relationship == STU_SKIPPED:
                quality = QUALITY_SKIPPED
                skipped = 1
                cancelled_stops += 1
            elif last.schedule_relationship == STU_NO_DATA:
                # Explicitly "no realtime information". Never read this as on time.
                quality = QUALITY_NO_DATA
            else:
                actual_arr, arr_delay = _resolve(last.arr_time_utc, last.arr_delay_s, supposed_arr)
                actual_dep, dep_delay = _resolve(last.dep_time_utc, last.dep_delay_s, supposed_dep)
                predicted = _predicted(last, event) or reference
                quality, poll_gap = _grade_stop(
                    last_observed_at=last.observed_at,
                    predicted=predicted,
                    polls=polls,
                    trip_seen_after_passage=trip_seen_after_passage,
                    last_seen=last_seen,
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
                "rule_version": RULE_VERSION,
                "n_observations": len(obs),
                "last_observed_at": obs[-1].observed_at if obs else None,
                "poll_gap_s": poll_gap,
                "cancelled": 0,
                "skipped": skipped,
                "supposed_departure_hour": local_hour(supposed_dep) if supposed_dep else None,
            }
        )

    first = rows[0] if rows else {}
    last_row = rows[-1] if rows else {}
    all_skipped = bool(rows) and cancelled_stops == len(rows)
    status = "CANCELLED" if all_skipped else ("COMPLETED" if n_observed else "MISSING")
    rollup = {
        "actual_departure_utc": first.get("actual_departure_utc"),
        "actual_arrival_utc": last_row.get("actual_arrival_utc"),
        "final_delay_s": last_row.get("arr_delay_s") or last_row.get("dep_delay_s"),
        "max_delay_s": max(delays) if delays else None,
        "n_stops_observed": n_observed,
        "status": status,
        "quality": _dominant_quality(rows),
    }
    return rows, rollup


def _grade_stop(
    *,
    last_observed_at: int,
    predicted: int | None,
    polls: list[int],
    trip_seen_after_passage: bool,
    last_seen: int | None,
    settle_s: int = SETTLE_S,
) -> tuple[str, int | None]:
    """Confidence in a single derived value.

    settle_s defaults to the global constant, which is right for GTFS-RT where one
    fetch covers the whole country at a fixed cadence. Timetables passes a
    per-station value instead: a station polled every 12 minutes cannot honestly
    call anything settled after 5.
    """
    if predicted is None:
        return QUALITY_PREDICTION_ONLY, None

    passage = predicted + GRACE_S
    settled_by = passage + settle_s

    # We were still polling after the value should have settled.
    if _polled_at_or_after(polls, settled_by):
        if last_observed_at >= passage:
            # The value was revised after the event and then held. Strongest evidence.
            return QUALITY_OBSERVED, None
        if trip_seen_after_passage:
            # The run was still reporting, just not about this stop -- so this stop's
            # value stood unchanged through its own passage.
            return QUALITY_INFERRED_DOWNSTREAM, None
        # The run stopped appearing before settling; carry its last word.
        return QUALITY_LAST_SEEN, None

    # Nobody was listening when it mattered. Record how wide the blind spot was.
    latest_poll = polls[-1] if polls else last_observed_at
    return QUALITY_PREDICTION_ONLY, max(0, settled_by - latest_poll)


def _resolve(
    time_utc: int | None, delay_s: int | None, supposed: int | None
) -> tuple[int | None, int | None]:
    """Absolute instant and delay for one event, from whichever fields were sent.

    The spec gives the absolute `time` precedence over `delay`; in this feed both are
    always populated together, so either path yields the same answer.
    """
    if supposed is None and time_utc is None:
        return None, None
    if time_utc is not None:
        return time_utc, (time_utc - supposed) if supposed is not None else delay_s
    if delay_s is not None and supposed is not None:
        return supposed + delay_s, delay_s
    return None, delay_s


def _predicted(obs: RtObservation, event: _Event) -> int | None:
    """When the feed last expected this stop's departure (or arrival) to happen."""
    if obs.dep_time_utc is not None:
        return obs.dep_time_utc
    if obs.arr_time_utc is not None:
        return obs.arr_time_utc
    if event.supposed_dep is not None and obs.dep_delay_s is not None:
        return event.supposed_dep + obs.dep_delay_s
    if event.supposed_arr is not None and obs.arr_delay_s is not None:
        return event.supposed_arr + obs.arr_delay_s
    return event.supposed_dep or event.supposed_arr


def _dominant_quality(rows: list[dict]) -> str | None:
    if not rows:
        return None
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["quality"]] += 1
    return max(counts.items(), key=lambda kv: kv[1])[0]
