"""Poll /fchg for the tracked stations and record what changed.

Follows realtime/poller.py step for step. The change-detection filter is not an
optimisation here but a necessity: /fchg returns *every* known change over a
many-hour horizon on every poll — in the pilot, ~2,400 stop events per sweep
against ~930 planned events in the next three hours — so storing unchanged
repeats would grow the table by the whole horizon every 30 seconds.

One tt_poll row is written per station per sweep, including failures. A station
timing out is data about our own coverage, and must not abort the sweep.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import case, func, insert, select, text, update
from sqlalchemy.orm import Session

from dbrel.gtfs.timeutil import local_date
from dbrel.models import Station, TrackedStation, Trip, TtObservation, TtPoll
from dbrel.timetables.client import TimetablesClient
from dbrel.timetables.parser import TtStop, parse_timetable


@dataclass(slots=True)
class PollResult:
    sweep_id: int = 0
    stations_polled: int = 0
    stations_failed: int = 0
    stops_seen: int = 0
    matched: int = 0
    unmatched: int = 0
    changed: int = 0
    messages: int = 0
    cancellations: int = 0
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)


def _candidate_service_dates(now: int) -> list[str]:
    """A run departing 23:50 is still going after midnight, so both days are live."""
    today = local_date(now)
    return [(today + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)]


def _load_trip_index(session: Session, service_dates: list[str]) -> dict[str, int]:
    """run_key -> trip.id. The run key already encodes the date, so it is unique."""
    rows = session.execute(
        select(Trip.source_run_key, Trip.id).where(
            Trip.service_date.in_(service_dates), Trip.source == "tts"
        )
    ).all()
    return {run_key: trip_id for run_key, trip_id in rows}


def _load_last_observations(session: Session, trip_ids: list[int]) -> dict[tuple[int, int], tuple]:
    """Latest stored value per (trip, stop). MAX(id) works: rows are append-only."""
    latest: dict[tuple[int, int], tuple] = {}
    if not trip_ids:
        return latest
    chunk_size = 900
    for start in range(0, len(trip_ids), chunk_size):
        chunk = trip_ids[start : start + chunk_size]
        rows = session.execute(
            select(
                TtObservation.trip_id,
                TtObservation.stop_sequence,
                TtObservation.arr_time_utc,
                TtObservation.arr_platform,
                TtObservation.arr_status,
                TtObservation.dep_time_utc,
                TtObservation.dep_platform,
                TtObservation.dep_status,
                TtObservation.cancelled_at_utc,
            ).where(
                TtObservation.id.in_(
                    select(func.max(TtObservation.id))
                    .where(TtObservation.trip_id.in_(chunk))
                    .group_by(TtObservation.trip_id, TtObservation.stop_sequence)
                )
            )
        ).all()
        for trip_id, seq, a_t, a_p, a_s, d_t, d_p, d_s, clt in rows:
            latest[(trip_id, seq)] = (a_t, a_p, a_s, clt, d_t, d_p, d_s, clt)
    return latest


def _observation_value(stop: TtStop) -> tuple:
    """Must line up with the column order loaded above."""
    ar, dp = stop.arrival, stop.departure
    return (
        ar.changed_ts if ar else None,
        ar.changed_platform if ar else None,
        ar.changed_status if ar else None,
        ar.cancelled_at if ar else None,
        dp.changed_ts if dp else None,
        dp.changed_platform if dp else None,
        dp.changed_status if dp else None,
        dp.cancelled_at if dp else None,
    )


def run_tt_poll(
    session: Session,
    client: TimetablesClient,
    *,
    priority_max: int = 3,
    sweep_index: int = 0,
    dry_run: bool = False,
    progress=lambda msg: None,
) -> PollResult:
    started = time.monotonic()
    now = int(time.time())
    result = PollResult(sweep_id=now)

    tracked = session.execute(
        select(TrackedStation, Station)
        .join(Station, Station.id == TrackedStation.station_id)
        .where(TrackedStation.removed_at.is_(None))
        .order_by(TrackedStation.poll_priority, TrackedStation.station_id)
    ).all()
    # Priority tiering: a station with priority N is polled every Nth sweep, so a
    # large set can keep high resolution where it matters within 60 rpm.
    tracked = [
        (ts, st)
        for ts, st in tracked
        if ts.poll_priority <= priority_max and sweep_index % max(1, ts.poll_priority) == 0
    ]
    if not tracked:
        result.errors.append("no tracked stations")
        return result

    service_dates = _candidate_service_dates(now)
    trip_by_run = _load_trip_index(session, service_dates)
    last_values = _load_last_observations(session, list(trip_by_run.values()))

    for tracked_station, station in tracked:
        fetch_started = time.monotonic()
        resp = client.changes(tracked_station.eva)
        poll = TtPoll(
            sweep_id=result.sweep_id,
            station_id=station.id,
            eva=tracked_station.eva,
            fetched_at=int(time.time()),
            http_status=resp.status,
            bytes=len(resp.body),
            fetch_ms=resp.elapsed_ms,
        )
        if not resp.ok:
            poll.error = resp.body[:200].decode("utf-8", "replace")
            session.add(poll)
            tracked_station.n_fchg_errors += 1
            result.stations_failed += 1
            result.errors.append(f"{station.name}: HTTP {resp.status}")
            continue

        try:
            parsed = parse_timetable(resp.body, keep=trip_by_run.__contains__)
        except Exception as exc:
            poll.error = f"parse: {exc}"
            session.add(poll)
            result.stations_failed += 1
            result.errors.append(f"{station.name}: {exc}")
            continue

        poll.content_sha256 = hashlib.sha256(resp.body).hexdigest()
        poll.parse_ms = parsed.parse_ms
        poll.n_stops = parsed.n_raw_stops
        session.add(poll)
        session.flush()

        store_started = time.monotonic()
        observed_at = int(time.time())
        obs_rows: list[dict] = []
        msg_rows: list[dict] = []
        seen_trip_ids: set[int] = set()
        # Per-station, not cumulative: these land on this station's poll row.
        station_matched = station_unmatched = 0

        for stop in parsed.stops:
            trip_id = trip_by_run.get(stop.run_key)
            if trip_id is None:
                # keep= already filtered these, so reaching here means the run key
                # changed shape; count it rather than dropping it silently.
                result.unmatched += 1
                station_unmatched += 1
                continue
            result.matched += 1
            station_matched += 1
            seen_trip_ids.add(trip_id)

            value = _observation_value(stop)
            if all(v is None for v in value):
                continue  # a stop present in fchg but carrying no change yet
            if last_values.get((trip_id, stop.stop_index)) == value:
                continue

            last_values[(trip_id, stop.stop_index)] = value
            ar, dp = stop.arrival, stop.departure
            obs_rows.append(
                {
                    "poll_id": poll.id,
                    "trip_id": trip_id,
                    "stop_sequence": stop.stop_index,
                    "observed_at": observed_at,
                    "arr_delay_s": ar.delay_s if ar else None,
                    "dep_delay_s": dp.delay_s if dp else None,
                    "arr_time_utc": ar.changed_ts if ar else None,
                    "dep_time_utc": dp.changed_ts if dp else None,
                    "arr_status": ar.changed_status if ar else None,
                    "dep_status": dp.changed_status if dp else None,
                    "arr_platform": (ar.changed_platform or ar.planned_platform) if ar else None,
                    "dep_platform": (dp.changed_platform or dp.planned_platform) if dp else None,
                    "cancelled_at_utc": (ar.cancelled_at if ar else None)
                    or (dp.cancelled_at if dp else None),
                    "changed_path": (ar.changed_path if ar else None)
                    or (dp.changed_path if dp else None),
                }
            )
            result.changed += 1
            if (ar and ar.is_cancelled) or (dp and dp.is_cancelled):
                result.cancellations += 1

            for message in stop.messages:
                msg_rows.append(
                    {
                        "trip_id": trip_id,
                        "stop_sequence": stop.stop_index,
                        "msg_id": message.msg_id,
                        "msg_type": message.msg_type,
                        "code": message.code,
                        "category": message.category,
                        "ts": message.ts,
                        "valid_from": message.valid_from,
                        "valid_to": message.valid_to,
                        "priority": message.priority,
                        "deleted": int(message.deleted),
                    }
                )

        if obs_rows and not dry_run:
            session.execute(insert(TtObservation), obs_rows)
        if msg_rows and not dry_run:
            # A message repeats on every poll while it is valid.
            session.execute(
                text(
                    "INSERT OR IGNORE INTO tt_message "
                    "(trip_id, stop_sequence, msg_id, msg_type, code, category, ts, "
                    " valid_from, valid_to, priority, deleted) "
                    "VALUES (:trip_id, :stop_sequence, :msg_id, :msg_type, :code, :category, "
                    " :ts, :valid_from, :valid_to, :priority, :deleted)"
                ),
                msg_rows,
            )
            result.messages += len(msg_rows)

        if seen_trip_ids and not dry_run:
            session.execute(
                update(Trip)
                .where(Trip.id.in_(list(seen_trip_ids)))
                .values(
                    status=case((Trip.status == "SCHEDULED", "RUNNING"), else_=Trip.status),
                    n_polls_seen=Trip.n_polls_seen + 1,
                )
            )

        poll.n_matched = station_matched
        poll.n_unmatched = station_unmatched
        poll.n_changed = len(obs_rows)
        poll.store_ms = int((time.monotonic() - store_started) * 1000)

        tracked_station.last_fchg_at = observed_at
        tracked_station.n_fchg_polls += 1
        result.stations_polled += 1
        result.stops_seen += parsed.n_raw_stops
        progress(
            f"    {station.name:<26} {parsed.n_raw_stops:>5} stops "
            f"{len(obs_rows):>4} changed  {int((time.monotonic() - fetch_started) * 1000)}ms"
        )

    session.flush()
    result.elapsed_s = time.monotonic() - started
    return result
