"""Resolving a run's identity and writing its schedule.

Shared by both Timetables importers, because both need it and neither can be the
only one to have it. /plan supplies the planned timetable for ordinary runs, but
it structurally cannot contain additions and replacements — and those are exactly
the entries /fchg sends as a *full* record rather than a delta, `pt` and `<tl>`
together. So /fchg has to be able to create a run too, and the identity rules
must be the same ones or the two sources would mint different trains for the
same service.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.config import TZ
from dbrel.identity import (
    IDENTITY_NUMBERED,
    IDENTITY_PATTERN,
    SOURCE_TT,
    line_aliases,
    line_display_name,
    line_identity_key,
    path_sha1,
    search_norm,
    segment_of,
    train_aliases,
    train_display_name,
    train_identity_key,
)
from dbrel.models import (
    FeedVersion,
    SearchAlias,
    ServiceLine,
    StationStop,
    Train,
    Trip,
)
from dbrel.ingest.parser import TtStop

TT_FEED_KIND = "tt"


@dataclass(slots=True)
class RunContext:
    """Everything resolving a run needs, loaded once per import rather than per run."""

    fv_id: int
    station_id_by_eva: dict[int, int]
    station_name_by_eva: dict[int, str]
    train_by_key: dict[str, int]
    line_cache: dict[str, int] = field(default_factory=dict)
    now: int = 0


@dataclass(slots=True)
class RunCounters:
    trains_created: int = 0
    trips_created: int = 0
    station_stops_written: int = 0
    conflicts: int = 0
    dst_fixups: int = 0


def tt_feed_version(session: Session) -> int:
    """One synthetic feed_version row for all Timetables data.

    station_stop.feed_version_id is NOT NULL with an FK, and rather than relax
    that, Timetables gets a single row. Deliberately not one per month: the
    finalize pattern lookup keys on (train_id, feed_version_id) and a monthly row
    would break it for any run spanning a month boundary.
    """
    existing = session.execute(
        select(FeedVersion.id).where(FeedVersion.kind == TT_FEED_KIND)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    now = int(time.time())
    row = FeedVersion(
        kind=TT_FEED_KIND,
        source_url="iris-tts",
        content_sha256=hashlib.sha256(b"tt:v1").hexdigest(),
        downloaded_at=now,
        imported_at=now,
        status="active",
    )
    session.add(row)
    session.flush()
    return row.id


def fix_dst(stops: list[TtStop]) -> int:
    """Add an hour to any stop whose time went backwards along the run.

    pt/ct are local wall clock with no offset, so on the autumn changeover 02:30
    occurs twice and parsing picks the first. Monotonicity along the run is the
    only available disambiguator.
    """
    fixed = 0
    last: int | None = None
    for stop in sorted(stops, key=lambda s: s.stop_index):
        for event in (stop.arrival, stop.departure):
            if event is None or event.planned_ts is None:
                continue
            if last is not None and event.planned_ts < last - 60:
                event.planned_ts += 3600
                fixed += 1
            last = event.planned_ts
    return fixed


def sec_of_day(epoch: int | None, service_date: str) -> int | None:
    """Seconds from the run's service-date anchor, GTFS-style (>86400 allowed).

    Uses the same noon-anchored convention as gtfs/timeutil so a stop after
    midnight reads as 88200 rather than wrapping to the small hours.
    """
    if epoch is None:
        return None
    day = date_cls.fromisoformat(service_date)
    anchor = datetime(day.year, day.month, day.day, 12, tzinfo=TZ).timestamp() - 12 * 3600
    return int(epoch - anchor)


def epoch_of_sec(sec: int | None, service_date: str) -> int | None:
    """Inverse of sec_of_day: an absolute instant from a schedule offset."""
    if sec is None:
        return None
    day = date_cls.fromisoformat(service_date)
    anchor = datetime(day.year, day.month, day.day, 12, tzinfo=TZ).timestamp() - 12 * 3600
    return int(anchor + sec)


def upsert_line(
    session: Session,
    cache: dict[str, int],
    category: str | None,
    line: str | None,
    operator: str | None,
) -> int | None:
    if not category or not line:
        return None
    key = line_identity_key(category, line)
    if key in cache:
        return cache[key]
    found = session.execute(
        select(ServiceLine.id).where(ServiceLine.identity_key == key)
    ).scalar_one_or_none()
    if found is None:
        display = line_display_name(category, line)
        row = ServiceLine(
            identity_key=key,
            category=category,
            line=line,
            operator=operator,
            segment=segment_of(category, line),
            display_name=display,
            search_name=search_norm(display),
        )
        session.add(row)
        session.flush()
        found = row.id
        for alias_norm, alias_display, kind, weight in line_aliases(category, line):
            session.add(
                SearchAlias(
                    alias_norm=alias_norm,
                    alias_display=alias_display,
                    kind=kind,
                    weight=weight,
                    line_id=found,
                )
            )
    cache[key] = found
    return found


def upsert_run(
    session: Session,
    ctx: RunContext,
    counters: RunCounters,
    run_key: str,
    stops: list[TtStop],
) -> int | None:
    """Resolve a run to a trip, creating train and schedule rows as needed.

    Returns the trip id. Only None if the run carries no usable stop at all.
    """
    stops = sorted(stops, key=lambda s: s.stop_index)
    counters.dst_fixups += fix_dst(stops)
    head = stops[0]
    station_name = ctx.station_name_by_eva.get(head.eva, "")
    full_path = head.full_path(station_name)
    digest = path_sha1(full_path)
    line_id = upsert_line(session, ctx.line_cache, head.category, head.line, head.owner)

    identity = train_identity_key(
        SOURCE_TT,
        head.category,
        head.number,
        path_hash=digest,
        start_hhmm=head.start_stamp[6:],
    )

    train_id = ctx.train_by_key.get(identity)
    if train_id is not None:
        train = session.get(Train, train_id)
        # Same number, different route AND different operator: two genuinely
        # different services sharing a number.
        if (
            train.path_sha1
            and train.path_sha1 != digest
            and train.operator
            and train.operator != head.owner
        ):
            counters.conflicts += 1
            # Park it on a variant identity rather than dropping it. Abandoning the
            # run here used to mean no trip existed, so /fchg could never match it
            # and the whole day's actuals for that service were lost silently.
            # The variant key is itself the record of the disagreement, which is why
            # the separate train_conflict audit table earned nothing and is gone.
            identity = f"{identity}#{digest[:8]}"
            train_id = ctx.train_by_key.get(identity)

    if train_id is None:
        # Deliberately NOT head.label: see Train.passenger_label.
        display = train_display_name(head.category, head.number)
        train = Train(
            source=SOURCE_TT,
            identity_kind=IDENTITY_NUMBERED if head.number else IDENTITY_PATTERN,
            identity_key=identity,
            train_number=int(head.number) if head.number and head.number.isdigit() else None,
            train_number_raw=head.number,
            category=head.category,
            operator=head.owner,
            trip_type=head.trip_type,
            segment=segment_of(head.category, head.line),
            line_id=line_id,
            display_name=display,
            search_name=search_norm(display),
            passenger_label=head.label,
            long_name=f"{display} {full_path[0]} → {full_path[-1]}" if full_path else display,
            origin_name=full_path[0] if full_path else None,
            dest_name=full_path[-1] if full_path else None,
            route_path="|".join(full_path),
            path_sha1=digest,
            n_stops=len(full_path),
            n_stops_tracked=len(stops),
            first_departure_sec=int(head.start_stamp[6:8]) * 3600
            + int(head.start_stamp[8:]) * 60,
            first_seen_date=head.service_date,
            last_seen_date=head.service_date,
            n_runs_seen=1,
            first_seen_feed_version_id=ctx.fv_id,
            last_seen_feed_version_id=ctx.fv_id,
        )
        session.add(train)
        session.flush()
        train_id = train.id
        ctx.train_by_key[identity] = train_id
        counters.trains_created += 1
        for alias_norm, alias_display, kind, weight in train_aliases(head.category, head.number):
            session.add(
                SearchAlias(
                    alias_norm=alias_norm,
                    alias_display=alias_display,
                    kind=kind,
                    weight=weight,
                    train_id=train_id,
                )
            )
    else:
        train = session.get(Train, train_id)
        train.last_seen_date = max(train.last_seen_date or "", head.service_date)
        train.n_runs_seen = (train.n_runs_seen or 0) + 1
        train.n_stops_tracked = max(train.n_stops_tracked or 0, len(stops))

    # --- station_stop: tracked stops only, at their true 1-based index ---
    existing_seqs = {
        row[0]
        for row in session.execute(
            select(StationStop.stop_sequence).where(
                StationStop.train_id == train_id,
                StationStop.feed_version_id == ctx.fv_id,
            )
        ).all()
    }
    ss_rows = []
    for stop in stops:
        if stop.stop_index in existing_seqs:
            continue
        station_id = ctx.station_id_by_eva.get(stop.eva)
        if station_id is None:
            continue
        side = stop.departure or stop.arrival
        ss_rows.append(
            {
                "feed_version_id": ctx.fv_id,
                "train_id": train_id,
                "stop_sequence": stop.stop_index,
                "station_id": station_id,
                "source_stop_id": str(stop.eva),
                "platform_code": side.planned_platform if side else None,
                "planned_arrival_sec": sec_of_day(stop.arrival.planned_ts, head.service_date)
                if stop.arrival
                else None,
                "planned_departure_sec": sec_of_day(
                    stop.departure.planned_ts, head.service_date
                )
                if stop.departure
                else None,
            }
        )
    if ss_rows:
        session.execute(insert(StationStop), ss_rows)
        counters.station_stops_written += len(ss_rows)

    # --- trip -------------------------------------------------------
    first_dep = next((s.departure.planned_ts for s in stops if s.departure), None)
    last_arr = next((s.arrival.planned_ts for s in reversed(stops) if s.arrival), None)

    # Trip carries two independent unique constraints -- uq_trip_train_date
    # (train_id, service_date) and uq_trip_src_run_date (source,
    # source_run_key, service_date) -- and an ON CONFLICT clause can only
    # target one of them. The ordinary case (re-importing the same run_key)
    # hits the second. But two different run_keys can legitimately resolve to
    # the same train_id on the same service_date -- observed live during a
    # full-day backfill, where a wide window groups the same physical run
    # slightly differently across buckets -- and that hits the first, which
    # this statement's ON CONFLICT does not cover, so the plain INSERT below
    # it raises IntegrityError instead of upserting. Checked explicitly first
    # because of that: if a trip already exists for this train on this
    # service_date under a different run_key, it IS this run's trip, so its
    # timing is refreshed in place rather than attempting a second, colliding
    # insert under the new run_key.
    existing_id = session.execute(
        select(Trip.id).where(Trip.train_id == train_id, Trip.service_date == head.service_date)
    ).scalar_one_or_none()
    if existing_id is not None:
        session.execute(
            update(Trip)
            .where(Trip.id == existing_id)
            .values(
                supposed_departure_utc=first_dep,
                supposed_arrival_utc=last_arr,
                n_stops_tracked=len(stops),
            )
        )
        counters.trips_created += 1
        return existing_id

    session.execute(
        sqlite_insert(Trip)
        .values(
            train_id=train_id,
            service_date=head.service_date,
            source=SOURCE_TT,
            source_run_key=run_key,
            daily_trip_id=head.daily_trip_id,
            feed_version_id=ctx.fv_id,
            status="SCHEDULED",
            schedule_relationship="PLANNED",
            supposed_departure_utc=first_dep,
            supposed_arrival_utc=last_arr,
            endpoints_tracked=int(len(stops) == len(full_path)),
            n_stops=len(full_path),
            n_stops_tracked=len(stops),
        )
        .on_conflict_do_update(
            index_elements=["source", "source_run_key", "service_date"],
            set_={
                "supposed_departure_utc": first_dep,
                "supposed_arrival_utc": last_arr,
                "n_stops_tracked": len(stops),
            },
        )
    )
    counters.trips_created += 1
    return session.execute(
        select(Trip.id).where(
            Trip.source == SOURCE_TT,
            Trip.source_run_key == run_key,
            Trip.service_date == head.service_date,
        )
    ).scalar_one()
