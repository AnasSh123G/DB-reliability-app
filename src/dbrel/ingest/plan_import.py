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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date as date_cls
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.identity import SOURCE_TT
from dbrel.config import TT_FETCH_WORKERS, TT_PLAN_LEAD_HOURS, TZ
from dbrel.models import (
    Station,
    Train,
    TtPlanFetch,
)
from dbrel.ingest.client import TimetablesClient
from dbrel.ingest.parser import TtStop, parse_timetable
from dbrel.ingest.timeutil import read_clock
from dbrel.ingest.dated_schedule import PlanReceipt
from dbrel.ingest.schedule_schema import require_dated_schedule_schema
from dbrel.ingest.runs import (
    RunContext,
    RunCounters,
    tt_feed_version,
    upsert_run,
)


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


def _pending_hours(
    last_hour: str | None, lead_hours: int, frozen: int | None = None
) -> list[tuple[str, int]]:
    """Hour buckets to fetch, as (YYYY-MM-DD, hour), oldest first."""
    now = datetime.fromtimestamp(read_clock(frozen), TZ).replace(
        minute=0, second=0, microsecond=0
    )
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


def import_plan(
    session: Session,
    client: TimetablesClient,
    *,
    lead_hours: int = TT_PLAN_LEAD_HOURS,
    date: str | None = None,
    hours: list[int] | None = None,
    force: bool = False,
    workers: int = TT_FETCH_WORKERS,
    now: int | None = None,
    progress=lambda msg: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
) -> PlanImportResult:
    require_dated_schedule_schema(session)
    started = time.monotonic()
    # None means live; an explicit value pins this import to a replayed instant.
    frozen = now
    result = PlanImportResult()
    fv_id = tt_feed_version(session)

    tracked = (
        session.execute(
            select(Station).where(Station.tracked == 1, Station.eva.isnot(None))
        )
        .scalars()
        .all()
    )
    if not tracked:
        result.errors.append("no tracked stations; run tt-map-stations first")
        return result

    station_name_by_eva = {st.eva: st.name for st in tracked}
    station_id_by_eva = {st.eva: st.id for st in tracked}

    # Collect every stop first, then resolve identities once across the batch:
    # a run seen at several tracked stations must produce ONE trip row.
    #
    # Keyed by stop_index rather than appended, because a stop straddling an hour
    # boundary (ar 22:59, dp 23:00) is returned in BOTH buckets. Last write wins;
    # the two copies are the same stop.
    by_run: dict[str, dict[int, TtStop]] = defaultdict(dict)
    observations_by_run: dict[str,list[tuple[TtStop,PlanReceipt]]] = defaultdict(list)

    # Computed up front, over every station, so on_progress can report a real
    # total from the first call -- each station's own bucket count differs in
    # rolling mode (each has its own last_plan_hour watermark).
    station_buckets: dict[int, list[tuple[str, int]]] = {
        station.id: (
            [(date, h) for h in (hours or range(24))]
            if date is not None
            else _pending_hours(station.last_plan_hour, lead_hours, frozen)
        )
        for station in tracked
    }
    total_buckets = sum(len(b) for b in station_buckets.values())
    done_buckets = 0
    on_progress(done_buckets, total_buckets)

    # Every existing fetch row for the window in one query rather than one SELECT
    # per bucket: a full-day backfill is 24 buckets x every tracked station, and at
    # TT_RPS_FAST the round trips would cost more than the fetching does.
    #
    # Loaded regardless of status (not just the 200 case): a bucket that previously
    # 404'd still has a row under the unique key (eva, plan_date, plan_hour), and a
    # caller that re-covers the same window on purpose — as the collect orchestrator
    # does, so an overlap never leaves a gap — must update that row rather than
    # insert a second one, or the unique constraint raises.
    days = {day for buckets in station_buckets.values() for day, _ in buckets}
    existing_by_key: dict[tuple[int, str, int], TtPlanFetch] = {}
    if days:
        for row in session.execute(
            select(TtPlanFetch).where(
                TtPlanFetch.plan_date.in_(days),
                TtPlanFetch.eva.in_([st.eva for st in tracked]),
            )
        ).scalars():
            existing_by_key[(row.eva, row.plan_date, row.plan_hour)] = row

    # Decide what needs the network before touching it, so the pool below is pure
    # I/O and every database access stays on this thread.
    pending: list[tuple[Station, str, int, TtPlanFetch | None]] = []
    for station in tracked:
        for day, hour in station_buckets[station.id]:
            existing = existing_by_key.get((station.eva, day, hour))
            if not force and existing is not None and existing.http_status == 200 and existing.error is None:
                result.buckets_skipped += 1
                done_buckets += 1
                on_progress(done_buckets, total_buckets)
                continue
            pending.append((station, day, hour, existing))

    # Plain ints and strings, resolved here rather than inside the worker: handing a
    # thread an ORM object risks it touching an expired attribute, which would emit a
    # lazy SELECT on a Session that is being written from this thread at the same time.
    fetch_args = [
        (station.eva, date_cls.fromisoformat(day).strftime("%y%m%d"), hour)
        for station, day, hour, _ in pending
    ]

    def fetch(args: tuple[int, str, int]):
        eva, yymmdd, hour = args
        return client.plan(eva, yymmdd, hour)

    # map() yields in submission order, which keeps last_plan_hour advancing
    # monotonically per station exactly as the sequential version did. The workers
    # only call client.plan(); parsing and every session touch happen right here.
    flushed_station_id: int | None = None
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for (station, day, hour, existing), resp in zip(pending, pool.map(fetch, fetch_args)):
            if flushed_station_id is not None and station.id != flushed_station_id:
                session.flush()
            flushed_station_id = station.id

            done_buckets += 1
            on_progress(done_buckets, total_buckets)

            fetch_row = existing or TtPlanFetch(eva=station.eva, plan_date=day, plan_hour=hour)
            fetch_row.fetched_at = resp.recorded_at if resp.recorded_at is not None else read_clock(frozen)
            fetch_row.http_status = resp.status
            fetch_row.content_sha256 = hashlib.sha256(resp.body).hexdigest() if resp.ok else None
            fetch_row.error = None
            fetch_row.n_stops = 0
            if not resp.ok:
                # 404 is routine: no service in this hour, or beyond the horizon.
                result.buckets_missing += 1
                fetch_row.error = (
                    None if resp.status == 404 else resp.body[:200].decode("utf-8", "replace")
                )
                session.add(fetch_row)
                continue

            try:
                parsed = parse_timetable(resp.body)
            except Exception as exc:
                fetch_row.error = f"parse: {exc}"
                session.add(fetch_row)
                result.errors.append(f"{station.name} {day}:{hour}: {exc}")
                parsed = None

            fetch_row.n_stops = len(parsed.stops) if parsed is not None else 0
            session.add(fetch_row)
            result.buckets_fetched += int(parsed is not None)
            result.stops_seen += fetch_row.n_stops
            # The current fetch summary stays latest-only, while dated evidence
            # preserves every original response in a replayed archive bucket.
            for original in (*resp.plan_history, resp):
                if original is resp and parsed is None:
                    continue
                try:
                    original_parsed = parsed if original is resp else parse_timetable(original.body)
                except Exception as exc:
                    result.errors.append(f"{station.name} {day}:{hour} history parse: {exc}")
                    continue
                receipt = PlanReceipt(
                    observed_at=original.recorded_at if original.recorded_at is not None else fetch_row.fetched_at,
                    content_sha256=hashlib.sha256(original.body).hexdigest(),
                    source_kind="plan", eva=station.eva, plan_date=day, plan_hour=hour,
                )
                for stop in original_parsed.stops:
                    if stop.eva is None:
                        stop = replace(stop, eva=station.eva)
                    by_run[stop.run_key][stop.stop_index] = stop
                    observations_by_run[stop.run_key].append((stop, receipt))

            station.last_plan_hour = f"{day}:{hour:02d}"
    session.flush()

    progress(f"  {result.buckets_fetched} buckets, {result.stops_seen} stops, {len(by_run)} runs")

    # --- resolve identities ------------------------------------------------
    # Shared with the /fchg importer: both create runs, and if they minted
    # identities differently the same service would end up as two trains.
    ctx = RunContext(
        fv_id=fv_id,
        station_id_by_eva=station_id_by_eva,
        station_name_by_eva=station_name_by_eva,
        train_by_key=dict(
            session.execute(
                # Only tts: keys can match; the 87k GTFS trains never will.
                select(Train.identity_key, Train.id).where(Train.source == SOURCE_TT)
            ).all()
        ),
        now=read_clock(frozen),
    )
    counters = RunCounters()

    for run_key, stops_by_index in by_run.items():
        upsert_run(session, ctx, counters, run_key, list(stops_by_index.values()),
                   schedule_observations=observations_by_run[run_key])

    result.trains_created = counters.trains_created
    result.trips_created = counters.trips_created
    result.station_stops_written = counters.station_stops_written
    result.conflicts = counters.conflicts
    result.dst_fixups = counters.dst_fixups
    line_cache = ctx.line_cache

    session.flush()
    result.lines_created = len(line_cache)
    result.elapsed_s = time.monotonic() - started
    return result
