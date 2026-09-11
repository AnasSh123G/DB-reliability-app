"""Import /plan into train / service_line / station_stop / trip.

/plan serves only today and tomorrow, and even tomorrow fills in gradually
(docs/probe-findings.md §15), so this rolls forward hour by hour rather than
batching a day. A bucket not captured inside its window is lost for good — there
is no backfill — which is why the GTFS skeleton remains the only source of "what
should have run" beyond tomorrow.

Only tracked stops become station_stop rows, at their true 1-based stopIndex, so
sequences are sparse but globally consistent. Adding a station later fills a gap
rather than renumbering anything, which is what lets the tracked set grow from 8
to hundreds without forking any history.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta

from sqlalchemy import insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.config import TT_PLAN_LEAD_HOURS, TZ
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
    Station,
    StationStop,
    TrackedStation,
    Train,
    TrainConflict,
    Trip,
    TtPlanFetch,
)
from dbrel.timetables.client import TimetablesClient
from dbrel.timetables.parser import TtStop, parse_timetable

TT_FEED_KIND = "tt"


@dataclass(slots=True)
class PlanImportResult:
    buckets_fetched: int = 0
    buckets_skipped: int = 0
    buckets_missing: int = 0
    stops_seen: int = 0
    trains_created: int = 0
    lines_created: int = 0
    trips_created: int = 0
    station_stops_written: int = 0
    conflicts: int = 0
    dst_fixups: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


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


def _pending_hours(last_hour: str | None, lead_hours: int) -> list[tuple[str, int]]:
    """Hour buckets to fetch, as (YYYY-MM-DD, hour), oldest first."""
    now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    horizon = now + timedelta(hours=lead_hours)
    cursor = now
    if last_hour:
        try:
            day, hour = last_hour.split(":")
            previous = datetime.fromisoformat(day).replace(hour=int(hour), tzinfo=TZ)
            cursor = min(max(previous + timedelta(hours=1), now - timedelta(hours=2)), now)
        except ValueError:
            pass
    out = []
    while cursor <= horizon:
        out.append((cursor.date().isoformat(), cursor.hour))
        cursor += timedelta(hours=1)
    return out


def _fix_dst(stops: list[TtStop]) -> int:
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


def _upsert_line(
    session: Session, cache: dict[str, int], category: str | None, line: str | None, operator: str | None
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


def import_plan(
    session: Session,
    client: TimetablesClient,
    *,
    lead_hours: int = TT_PLAN_LEAD_HOURS,
    date: str | None = None,
    hours: list[int] | None = None,
    force: bool = False,
    progress=lambda msg: None,
) -> PlanImportResult:
    started = time.monotonic()
    result = PlanImportResult()
    fv_id = tt_feed_version(session)

    tracked = session.execute(
        select(TrackedStation, Station)
        .join(Station, Station.id == TrackedStation.station_id)
        .where(TrackedStation.removed_at.is_(None))
    ).all()
    if not tracked:
        result.errors.append("no tracked stations; run tt-map-stations first")
        return result

    station_name_by_eva = {ts.eva: st.name for ts, st in tracked}
    station_id_by_eva = {ts.eva: st.id for ts, st in tracked}

    # Collect every stop first, then resolve identities once across the batch:
    # a run seen at several tracked stations must produce ONE trip row.
    #
    # Keyed by stop_index rather than appended, because a stop straddling an hour
    # boundary (ar 22:59, dp 23:00) is returned in BOTH buckets. Last write wins;
    # the two copies are the same stop.
    by_run: dict[str, dict[int, TtStop]] = defaultdict(dict)

    for tracked_station, station in tracked:
        if date is not None:
            buckets = [(date, h) for h in (hours or range(24))]
        else:
            buckets = _pending_hours(tracked_station.last_plan_hour, lead_hours)

        for day, hour in buckets:
            if not force:
                already = session.execute(
                    select(TtPlanFetch.id).where(
                        TtPlanFetch.eva == tracked_station.eva,
                        TtPlanFetch.plan_date == day,
                        TtPlanFetch.plan_hour == hour,
                        TtPlanFetch.http_status == 200,
                    )
                ).scalar_one_or_none()
                if already is not None:
                    result.buckets_skipped += 1
                    continue

            yymmdd = date_cls.fromisoformat(day).strftime("%y%m%d")
            resp = client.plan(tracked_station.eva, yymmdd, hour)
            fetch = TtPlanFetch(
                eva=tracked_station.eva,
                plan_date=day,
                plan_hour=hour,
                fetched_at=int(time.time()),
                http_status=resp.status,
                content_sha256=hashlib.sha256(resp.body).hexdigest() if resp.ok else None,
            )
            if not resp.ok:
                # 404 is routine: no service in this hour, or beyond the horizon.
                result.buckets_missing += 1
                fetch.error = None if resp.status == 404 else resp.body[:200].decode("utf-8", "replace")
                session.add(fetch)
                continue

            try:
                parsed = parse_timetable(resp.body)
            except Exception as exc:
                fetch.error = f"parse: {exc}"
                session.add(fetch)
                result.errors.append(f"{station.name} {day}:{hour}: {exc}")
                continue

            fetch.n_stops = len(parsed.stops)
            session.add(fetch)
            result.buckets_fetched += 1
            result.stops_seen += len(parsed.stops)
            for stop in parsed.stops:
                if stop.eva is None:
                    stop.eva = tracked_station.eva
                by_run[stop.run_key][stop.stop_index] = stop

            tracked_station.last_plan_hour = f"{day}:{hour:02d}"
        session.flush()

    progress(f"  {result.buckets_fetched} buckets, {result.stops_seen} stops, {len(by_run)} runs")

    # --- resolve identities ------------------------------------------------
    train_by_key = dict(session.execute(select(Train.identity_key, Train.id)).all())
    line_cache: dict[str, int] = {}
    now = int(time.time())

    for run_key, stops_by_index in by_run.items():
        stops = sorted(stops_by_index.values(), key=lambda s: s.stop_index)
        result.dst_fixups += _fix_dst(stops)
        head = stops[0]
        station_name = station_name_by_eva.get(head.eva, "")
        full_path = head.full_path(station_name)
        digest = path_sha1(full_path)
        line_id = _upsert_line(session, line_cache, head.category, head.line, head.owner)

        numbered = bool(head.number)
        identity = train_identity_key(
            SOURCE_TT,
            head.category,
            head.number,
            path_hash=digest,
            start_hhmm=head.start_stamp[6:],
        )

        train_id = train_by_key.get(identity)
        if train_id is None:
            # Deliberately NOT head.label: see Train.passenger_label.
            display = train_display_name(head.category, head.number)
            train = Train(
                source=SOURCE_TT,
                identity_kind=IDENTITY_NUMBERED if numbered else IDENTITY_PATTERN,
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
                first_departure_sec=int(head.start_stamp[6:8]) * 3600 + int(head.start_stamp[8:]) * 60,
                first_seen_date=head.service_date,
                last_seen_date=head.service_date,
                n_runs_seen=1,
                first_seen_feed_version_id=fv_id,
                last_seen_feed_version_id=fv_id,
            )
            session.add(train)
            session.flush()
            train_id = train.id
            train_by_key[identity] = train_id
            result.trains_created += 1
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
            # Same number, different route and different operator: two genuinely
            # different services sharing a number. Park it and make it queryable
            # rather than silently merging two histories.
            if (
                train.path_sha1
                and train.path_sha1 != digest
                and train.operator
                and train.operator != head.owner
            ):
                session.add(
                    TrainConflict(
                        train_id=train_id,
                        service_date=head.service_date,
                        field="path_sha1+operator",
                        expected=f"{train.path_sha1[:12]} / {train.operator}",
                        observed=f"{digest[:12]} / {head.owner}",
                        run_key=run_key,
                        first_seen=now,
                    )
                )
                result.conflicts += 1
                continue
            train.last_seen_date = max(train.last_seen_date or "", head.service_date)
            train.n_runs_seen = (train.n_runs_seen or 0) + 1
            train.n_stops_tracked = max(train.n_stops_tracked or 0, len(stops))

        # --- station_stop: tracked stops only, at their true 1-based index ---
        existing_seqs = {
            row[0]
            for row in session.execute(
                select(StationStop.stop_sequence).where(
                    StationStop.train_id == train_id, StationStop.feed_version_id == fv_id
                )
            ).all()
        }
        ss_rows = []
        for stop in stops:
            if stop.stop_index in existing_seqs:
                continue
            station_id = station_id_by_eva.get(stop.eva)
            if station_id is None:
                continue
            ss_rows.append(
                {
                    "feed_version_id": fv_id,
                    "train_id": train_id,
                    "stop_sequence": stop.stop_index,
                    "station_id": station_id,
                    "source_stop_id": str(stop.eva),
                    "platform_code": (stop.departure or stop.arrival).planned_platform
                    if (stop.departure or stop.arrival)
                    else None,
                    "planned_arrival_sec": _sec_of_day(stop.arrival.planned_ts, head)
                    if stop.arrival
                    else None,
                    "planned_departure_sec": _sec_of_day(stop.departure.planned_ts, head)
                    if stop.departure
                    else None,
                }
            )
        if ss_rows:
            session.execute(insert(StationStop), ss_rows)
            result.station_stops_written += len(ss_rows)

        # --- trip -------------------------------------------------------
        first_dep = next((s.departure.planned_ts for s in stops if s.departure), None)
        last_arr = next((s.arrival.planned_ts for s in reversed(stops) if s.arrival), None)
        stmt = (
            sqlite_insert(Trip)
            .values(
                train_id=train_id,
                service_date=head.service_date,
                source=SOURCE_TT,
                source_run_key=run_key,
                daily_trip_id=head.daily_trip_id,
                feed_version_id=fv_id,
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
        session.execute(stmt)
        result.trips_created += 1

    session.flush()
    result.lines_created = len(line_cache)
    result.elapsed_s = time.monotonic() - started
    return result


def _sec_of_day(epoch: int | None, head: TtStop) -> int | None:
    """Seconds from the run's service-date anchor, GTFS-style (>86400 allowed).

    Uses the same noon-anchored convention as gtfs/timeutil so a stop after
    midnight reads as 88200 rather than wrapping to the small hours.
    """
    if epoch is None:
        return None
    day = date_cls.fromisoformat(head.service_date)
    anchor = datetime(day.year, day.month, day.day, 12, tzinfo=TZ).timestamp() - 12 * 3600
    return int(epoch - anchor)
