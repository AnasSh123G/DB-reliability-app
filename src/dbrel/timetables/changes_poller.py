"""Poll /fchg for the tracked stations and record what has already happened.

This is an archival reader, not a live monitor. /fchg returns its whole horizon on
every poll — forecasts for trains still to come, alongside recorded values for
trains already gone — and only the second kind is wanted here. IRIS never labels
which is which (`ct` is an estimate before passage and DB's recorded actual
afterwards), so the distinction is drawn from the clock: an event whose current
time is far enough in the past has happened, and its `ct` is therefore a fact.

That filter is judged on `ct`, never `pt`. A train booked for 10:00 and running
30 minutes late has not departed at 10:15, and grading it against its planned
time would invent a departure that never occurred.

One tt_poll row is written per station per sweep, including failures. A station
timing out is data about our own coverage, and must not abort the sweep.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import case, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.identity import SOURCE_TT
from dbrel.config import GRACE_S
from dbrel.gtfs.timeutil import local_date
from dbrel.models import (
    Station,
    TrackedStation,
    Train,
    Trip,
    TtObservation,
    TtPoll,
    TtUnmatched,
)
from dbrel.timetables.client import TimetablesClient
from dbrel.timetables.parser import TtEvent, TtStop, parse_timetable
from dbrel.timetables.runs import (
    RunContext,
    RunCounters,
    tt_feed_version,
    upsert_run,
)

# IRIS returns DELFI bus and tram halts alongside rail. This project is rail only.
NON_RAIL_CATEGORIES = frozenset({"Bus", "STR", "STB", "U"})


@dataclass(slots=True)
class PollResult:
    sweep_id: int = 0
    stations_polled: int = 0
    stations_failed: int = 0
    stops_seen: int = 0
    matched: int = 0
    unmatched: int = 0
    created: int = 0
    pending: int = 0
    stored: int = 0
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


def _is_full_record(stop: TtStop) -> bool:
    """Whether /fchg sent this stop complete enough to build a run from.

    For an ordinary planned run /fchg is a pure delta — ct with no pt — and the
    planned side has to come from /plan. But additions and replacements, which
    /plan structurally never contains, arrive with <tl> and pt together (they
    co-occur exactly in the samples). Those are the runs worth creating here,
    and the only ones that can be.
    """
    if not stop.category or not stop.number:
        return False
    if stop.category in NON_RAIL_CATEGORIES:
        return False
    return any(
        side is not None and side.planned_ts is not None
        for side in (stop.arrival, stop.departure)
    )


def _has_happened(event: TtEvent | None, now: int) -> bool:
    """Whether this arrival or departure is already in the past.

    The reference is the current published time, falling back to the planned one
    when IRIS has said nothing — never the other way round.
    """
    if event is None:
        return False
    reference = event.changed_ts or event.planned_ts
    if reference is None:
        return False
    return reference + GRACE_S < now


def _existing_value_shas(session: Session, trip_ids: list[int]) -> set[tuple[int, int, str]]:
    """Values already recorded for these runs, as (trip, stop, digest).

    Answered from the uniqueness index alone, so it stays cheap as history grows.
    """
    known: set[tuple[int, int, str]] = set()
    chunk_size = 900  # SQLite's default parameter ceiling is 999
    for start in range(0, len(trip_ids), chunk_size):
        rows = session.execute(
            select(
                TtObservation.trip_id,
                TtObservation.stop_sequence,
                TtObservation.value_sha,
            ).where(TtObservation.trip_id.in_(trip_ids[start : start + chunk_size]))
        ).all()
        known.update(rows)
    return known


def _value_sha(ar: TtEvent | None, dp: TtEvent | None) -> str:
    """Digest of the published value, for database-level dedup.

    Comparing tuples in Python is what this replaces: the written row, the row read
    back and the value compared had drifted into three different column orders,
    which leaked 34% duplicates and corrupted observed_at into "when we last
    rewrote an unchanged value" — the field grading reads as "when last revised".
    """
    parts = []
    for event in (ar, dp):
        parts.extend(
            ("", "", "", "")
            if event is None
            else (
                event.changed_ts,
                event.changed_platform,
                event.changed_status,
                event.cancelled_at,
            )
        )
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


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

    # Shared with the /plan importer so both mint identities the same way.
    run_ctx = RunContext(
        fv_id=tt_feed_version(session),
        station_id_by_eva={ts.eva: st.id for ts, st in tracked},
        station_name_by_eva={ts.eva: st.name for ts, st in tracked},
        train_by_key=dict(
            session.execute(
                # Only tts: keys can match; the 87k GTFS trains never will.
                select(Train.identity_key, Train.id).where(Train.source == SOURCE_TT)
            ).all()
        ),
        now=now,
    )
    run_counters = RunCounters()

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
            # No keep= predicate: filtering by known run key used to drop unmatched
            # runs inside the parser, before an object existed to count. That hid
            # 80% of returned events behind a counter that could never fire.
            parsed = parse_timetable(resp.body)
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
        unmatched_rows: list[dict] = []
        seen_trip_ids: set[int] = set()
        # Per-station, not cumulative: these land on this station's poll row.
        station_matched = station_unmatched = station_created = 0

        for stop in parsed.stops:
            trip_id = trip_by_run.get(stop.run_key)
            if trip_id is None and _is_full_record(stop):
                # An addition or replacement. /plan will never carry it, so this is
                # the only chance to record it at all.
                if stop.eva is None:
                    stop.eva = tracked_station.eva
                trip_id = upsert_run(
                    session, run_ctx, run_counters, stop.run_key, [stop]
                )
                trip_by_run[stop.run_key] = trip_id
                station_created += 1
            if trip_id is None:
                # A delta for a run we have no /plan row for. Recorded rather than
                # dropped: a rising count is the alarm that the roller has fallen
                # behind, and because /plan serves only today and tomorrow that
                # planned data is being lost permanently.
                station_unmatched += 1
                unmatched_rows.append(
                    {
                        "service_date": stop.service_date,
                        "daily_trip_id": stop.daily_trip_id,
                        "stop_index": stop.stop_index,
                        "eva": stop.eva or tracked_station.eva,
                        "category": stop.category,
                        "number": stop.number,
                        "first_poll_id": poll.id,
                        "last_poll_id": poll.id,
                    }
                )
                continue
            station_matched += 1
            seen_trip_ids.add(trip_id)

            # Only the side that has actually happened contributes. A stop whose
            # arrival is past but whose departure is still ahead yields its arrival
            # now and its departure on a later poll.
            ar = stop.arrival if _has_happened(stop.arrival, now) else None
            dp = stop.departure if _has_happened(stop.departure, now) else None
            if ar is None and dp is None:
                result.pending += 1
                continue
            if (ar is None or ar.changed_ts is None) and (dp is None or dp.changed_ts is None):
                # Past, but IRIS published no time for it — nothing to record.
                continue

            obs_rows.append(
                {
                    "poll_id": poll.id,
                    "trip_id": trip_id,
                    "stop_sequence": stop.stop_index,
                    "observed_at": observed_at,
                    "value_sha": _value_sha(ar, dp),
                    "arr_delay_s": ar.delay_s if ar else None,
                    "dep_delay_s": dp.delay_s if dp else None,
                    "arr_time_utc": ar.changed_ts if ar else None,
                    "dep_time_utc": dp.changed_ts if dp else None,
                    "arr_status": ar.changed_status if ar else None,
                    "dep_status": dp.changed_status if dp else None,
                    # cp verbatim. The planned platform lives on station_stop.
                    "arr_platform": ar.changed_platform if ar else None,
                    "dep_platform": dp.changed_platform if dp else None,
                    "cancelled_at_utc": (ar.cancelled_at if ar else None)
                    or (dp.cancelled_at if dp else None),
                    "changed_path": (ar.changed_path if ar else None)
                    or (dp.changed_path if dp else None),
                }
            )
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

        # An already-recorded value reappears on every poll for as long as it
        # lingers in the horizon, so most of obs_rows is usually already stored.
        # Filtering against what exists gives an honest stored count; the conflict
        # clause below is then only a guard against a concurrent sweep.
        known = _existing_value_shas(session, sorted({r["trip_id"] for r in obs_rows}))
        obs_rows = [
            r
            for r in obs_rows
            if (r["trip_id"], r["stop_sequence"], r["value_sha"]) not in known
        ]
        stored = len(obs_rows)
        if obs_rows and not dry_run:
            session.execute(
                sqlite_insert(TtObservation).on_conflict_do_nothing(
                    index_elements=["trip_id", "stop_sequence", "value_sha"]
                ),
                obs_rows,
            )
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
        if unmatched_rows and not dry_run:
            session.execute(
                sqlite_insert(TtUnmatched)
                .on_conflict_do_update(
                    index_elements=["service_date", "daily_trip_id", "stop_index"],
                    set_={
                        "last_poll_id": poll.id,
                        "n_polls": TtUnmatched.__table__.c.n_polls + 1,
                    },
                )
                .values(unmatched_rows)
            )

        if seen_trip_ids and not dry_run:
            session.execute(
                update(Trip)
                .where(Trip.id.in_(list(seen_trip_ids)))
                .values(
                    status=case((Trip.status == "SCHEDULED", "RUNNING"), else_=Trip.status),
                    n_polls_seen=Trip.n_polls_seen + 1,
                )
            )

        result.matched += station_matched
        result.unmatched += station_unmatched
        result.created += station_created
        result.stored += stored
        poll.n_matched = station_matched
        poll.n_unmatched = station_unmatched
        poll.n_changed = stored
        poll.store_ms = int((time.monotonic() - store_started) * 1000)

        tracked_station.last_fchg_at = observed_at
        tracked_station.n_fchg_polls += 1
        result.stations_polled += 1
        result.stops_seen += parsed.n_raw_stops
        progress(
            f"    {station.name:<26} {parsed.n_raw_stops:>5} stops "
            f"{station_matched:>5} matched {stored:>4} stored  "
            f"{int((time.monotonic() - fetch_started) * 1000)}ms"
        )

    session.flush()
    result.elapsed_s = time.monotonic() - started
    return result
