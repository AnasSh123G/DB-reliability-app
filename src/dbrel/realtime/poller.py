"""One fetch of the realtime feed, recorded as observations.

Realtime descriptors in this feed carry only trip_id and start_date -- no route_id,
no start_time -- so resolution is a direct lookup and there is no fallback. Anything
unresolvable is recorded in rt_unmatched rather than dropped, because a silently
shrinking match rate is the failure mode that would invalidate the whole dataset.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from sqlalchemy import func, insert, select, text
from sqlalchemy.orm import Session

from dbrel.config import HTTP_TIMEOUT_S, REALTIME_URL
from dbrel.db import Base
from dbrel.gtfs.timeutil import local_date, parse_service_date
from dbrel.models import RtObservation, RtPoll, RtUnmatched, Trip
from dbrel.realtime.parser import ParsedFeed, parse_feed


@dataclass
class PollResult:
    poll_id: int | None = None
    dry_run: bool = False
    fetched_bytes: int = 0
    feed_timestamp: int = 0
    feed_age_s: int = 0
    n_entities: int = 0
    n_trip_updates: int = 0
    n_candidates: int = 0
    n_matched: int = 0
    n_unmatched: int = 0
    n_stop_updates: int = 0
    n_changed: int = 0
    fetch_ms: int = 0
    parse_ms: int = 0
    store_ms: int = 0
    error: str | None = None
    service_dates: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.error:
            return f"poll failed: {self.error}"
        total = self.n_matched + self.n_unmatched
        rate = self.n_matched / total if total else 0.0
        head = "dry run" if self.dry_run else f"poll {self.poll_id}"
        return (
            f"\n{head}: {self.fetched_bytes / 1e6:.1f}MB, feed {self.feed_age_s}s old\n"
            f"  trip updates {self.n_trip_updates:,} -> rail candidates {self.n_candidates:,}\n"
            f"  matched to known runs {self.n_matched:,} ({rate:.1%}), unmatched {self.n_unmatched:,}\n"
            f"  stop updates {self.n_stop_updates:,}, changed since last poll {self.n_changed:,}\n"
            f"  timing: fetch {self.fetch_ms}ms  parse {self.parse_ms}ms  store {self.store_ms}ms"
        )


def fetch_realtime(url: str = REALTIME_URL) -> tuple[bytes, int, int]:
    """Returns (body, http_status, elapsed_ms). ~47 MB; gtfs.de offers no gzip."""
    started = time.monotonic()
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        body = response.content
    return body, response.status_code, int((time.monotonic() - started) * 1000)


def _candidate_service_dates(feed_timestamp: int) -> list[str]:
    """Service dates a running trip could belong to.

    A trip departing 23:50 is still running after midnight, so both today and
    yesterday are live. A day either side covers timezone and rollover edges.
    """
    today = local_date(feed_timestamp)
    return [(today + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)]


def _load_trip_index(session: Session, service_dates: list[str]) -> dict[tuple[str, str], int]:
    """(gtfs_trip_id, service_date) -> trip.id for the live service dates."""
    rows = session.execute(
        select(Trip.gtfs_trip_id, Trip.service_date, Trip.id).where(
            Trip.service_date.in_(service_dates)
        )
    ).all()
    return {(gtfs_id, date): trip_id for gtfs_id, date, trip_id in rows}


def _load_last_observations(
    session: Session, trip_ids: list[int]
) -> dict[tuple[int, int], tuple]:
    """Latest stored value per (trip, stop), for change detection.

    Uses MAX(id) per group: observations are append-only, so the highest id is the
    most recent. The (trip_id, stop_sequence, ...) index makes this a scan of only
    the trips in this poll.
    """
    latest: dict[tuple[int, int], tuple] = {}
    if not trip_ids:
        return latest
    chunk_size = 900
    for start in range(0, len(trip_ids), chunk_size):
        chunk = trip_ids[start : start + chunk_size]
        rows = session.execute(
            select(
                RtObservation.trip_id,
                RtObservation.stop_sequence,
                RtObservation.arr_delay_s,
                RtObservation.dep_delay_s,
                RtObservation.arr_time_utc,
                RtObservation.dep_time_utc,
                RtObservation.schedule_relationship,
            ).where(
                RtObservation.id.in_(
                    select(func.max(RtObservation.id))
                    .where(RtObservation.trip_id.in_(chunk))
                    .group_by(RtObservation.trip_id, RtObservation.stop_sequence)
                )
            )
        ).all()
        for trip_id, seq, arr_d, dep_d, arr_t, dep_t, rel in rows:
            latest[(trip_id, seq)] = (arr_d, dep_d, arr_t, dep_t, rel)
    return latest


def run_poll(
    session: Session,
    *,
    dry_run: bool = False,
    url: str = REALTIME_URL,
    progress=lambda msg: None,
) -> PollResult:
    Base.metadata.create_all(session.get_bind())
    result = PollResult(dry_run=dry_run)

    progress("fetching realtime feed ...")
    try:
        body, status, fetch_ms = fetch_realtime(url)
    except Exception as exc:  # noqa: BLE001 - a failed poll is data, not a crash
        result.error = f"{type(exc).__name__}: {exc}"
        if not dry_run:
            poll = RtPoll(fetched_at=int(time.time()), error=result.error)
            session.add(poll)
            session.flush()
            result.poll_id = poll.id
        return result

    result.fetched_bytes = len(body)
    result.fetch_ms = fetch_ms
    fetched_at = int(time.time())

    # The filter set has to exist before parsing, so that bus and tram trips are
    # discarded without ever becoming objects. Candidate dates come from the fetch
    # time rather than the feed header: the header is seconds old at most, and the
    # window spans a day either side regardless.
    service_dates = _candidate_service_dates(fetched_at)
    result.service_dates = service_dates
    progress(f"  resolving against service dates {', '.join(service_dates)}")
    trip_index = _load_trip_index(session, service_dates)
    known_ids = {gtfs_id for gtfs_id, _ in trip_index}
    progress(f"  {len(trip_index):,} known runs ({len(known_ids):,} distinct trip ids)")

    parsed: ParsedFeed = parse_feed(body, keep=known_ids.__contains__)
    result.feed_timestamp = parsed.timestamp
    result.feed_age_s = max(0, fetched_at - parsed.timestamp)
    result.n_entities = parsed.n_entities
    result.n_trip_updates = parsed.n_trip_updates
    result.n_candidates = len(parsed.trips)
    result.parse_ms = parsed.parse_ms
    result.n_stop_updates = parsed.n_stop_updates
    progress(
        f"  parsed in {parsed.parse_ms}ms: {parsed.n_trip_updates:,} trip updates,"
        f" {len(parsed.trips):,} on known rail trips"
    )

    # Resolve descriptors to dated runs.
    resolved: list[tuple[int, object]] = []
    unmatched: list[object] = []
    for trip in parsed.trips:
        service_date = (
            parse_service_date(trip.start_date).isoformat() if trip.start_date else None
        )
        trip_pk = trip_index.get((trip.trip_id, service_date)) if service_date else None
        if trip_pk is None:
            # start_date missing or naming a date we have no run for.
            for candidate in service_dates:
                trip_pk = trip_index.get((trip.trip_id, candidate))
                if trip_pk is not None:
                    break
        if trip_pk is None:
            unmatched.append(trip)
        else:
            resolved.append((trip_pk, trip))

    result.n_matched = len(resolved)
    result.n_unmatched = len(unmatched)

    store_started = time.monotonic()
    if dry_run:
        latest = _load_last_observations(session, [pk for pk, _ in resolved])
        result.n_changed = sum(
            1
            for pk, trip in resolved
            for stop in trip.stops
            if latest.get((pk, stop.stop_sequence)) != stop.value()
        )
        result.store_ms = int((time.monotonic() - store_started) * 1000)
        return result

    poll = RtPoll(
        fetched_at=fetched_at,
        feed_timestamp=parsed.timestamp,
        http_status=status,
        bytes=len(body),
        content_sha256=hashlib.sha256(body).hexdigest(),
        n_entities=parsed.n_entities,
        n_trip_updates=parsed.n_trip_updates,
        n_matched=len(resolved),
        n_unmatched=len(unmatched),
        n_stop_updates=parsed.n_stop_updates,
        fetch_ms=fetch_ms,
        parse_ms=parsed.parse_ms,
    )
    session.add(poll)
    session.flush()
    result.poll_id = poll.id

    progress("  comparing against last stored values ...")
    latest = _load_last_observations(session, [pk for pk, _ in resolved])

    rows = []
    for trip_pk, trip in resolved:
        for stop in trip.stops:
            key = (trip_pk, stop.stop_sequence)
            if latest.get(key) == stop.value():
                continue  # unchanged: republishing it would carry no information
            rows.append(
                {
                    "poll_id": poll.id,
                    "trip_id": trip_pk,
                    "stop_sequence": stop.stop_sequence,
                    "observed_at": parsed.timestamp,
                    "arr_delay_s": stop.arr_delay_s,
                    "dep_delay_s": stop.dep_delay_s,
                    "arr_time_utc": stop.arr_time_utc,
                    "dep_time_utc": stop.dep_time_utc,
                    "schedule_relationship": stop.schedule_relationship,
                }
            )

    progress(f"  writing {len(rows):,} changed observations ...")
    for start in range(0, len(rows), 5000):
        session.execute(insert(RtObservation), rows[start : start + 5000])
    result.n_changed = len(rows)
    poll.n_changed = len(rows)

    # Mark the runs we saw, so finalize can tell "no data" from "never appeared".
    seen_pks = [pk for pk, _ in resolved]
    for start in range(0, len(seen_pks), 500):
        chunk = seen_pks[start : start + 500]
        session.execute(
            Trip.__table__.update()
            .where(Trip.id.in_(chunk), Trip.status.in_(("SCHEDULED", "RUNNING")))
            .values(status="RUNNING", n_polls_seen=Trip.n_polls_seen + 1)
        )

    _record_unmatched(session, unmatched, poll.id, service_dates)

    poll.store_ms = int((time.monotonic() - store_started) * 1000)
    result.store_ms = poll.store_ms
    return result


def _record_unmatched(session: Session, trips: list, poll_id: int, service_dates: list[str]) -> None:
    """Upsert unresolvable descriptors, counting repeat sightings."""
    if not trips:
        return
    fallback = service_dates[1] if len(service_dates) > 1 else service_dates[0]
    payload = {}
    for trip in trips:
        date = parse_service_date(trip.start_date).isoformat() if trip.start_date else fallback
        payload[(date, trip.trip_id)] = {
            "service_date": date,
            "gtfs_trip_id": trip.trip_id,
            "gtfs_route_id": trip.route_id or None,
            "start_time": trip.start_time or None,
            "n_stop_updates": len(trip.stops),
            "first_poll_id": poll_id,
            "last_poll_id": poll_id,
            "n_polls": 1,
        }

    session.execute(
        text(
            """
            INSERT INTO rt_unmatched
                (service_date, gtfs_trip_id, gtfs_route_id, start_time,
                 n_stop_updates, first_poll_id, last_poll_id, n_polls)
            VALUES (:service_date, :gtfs_trip_id, :gtfs_route_id, :start_time,
                    :n_stop_updates, :first_poll_id, :last_poll_id, 1)
            ON CONFLICT (service_date, gtfs_trip_id) DO UPDATE SET
                n_polls = n_polls + 1,
                last_poll_id = excluded.last_poll_id,
                n_stop_updates = excluded.n_stop_updates
            """
        ),
        list(payload.values()),
    )
