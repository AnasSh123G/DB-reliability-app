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
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.identity import SOURCE_TT
from dbrel.config import TT_PLAN_LEAD_HOURS, TZ
from dbrel.models import (
    Station,
    Train,
    TtPlanFetch,
)
from dbrel.timetables.client import TimetablesClient
from dbrel.timetables.parser import TtStop, parse_timetable
from dbrel.timetables.runs import (
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


def import_plan(
    session: Session,
    client: TimetablesClient,
    *,
    lead_hours: int = TT_PLAN_LEAD_HOURS,
    date: str | None = None,
    hours: list[int] | None = None,
    force: bool = False,
    progress=lambda msg: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
) -> PlanImportResult:
    started = time.monotonic()
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

    # Computed up front, over every station, so on_progress can report a real
    # total from the first call -- each station's own bucket count differs in
    # rolling mode (each has its own last_plan_hour watermark).
    station_buckets: dict[int, list[tuple[str, int]]] = {
        station.id: (
            [(date, h) for h in (hours or range(24))]
            if date is not None
            else _pending_hours(station.last_plan_hour, lead_hours)
        )
        for station in tracked
    }
    total_buckets = sum(len(b) for b in station_buckets.values())
    done_buckets = 0
    on_progress(done_buckets, total_buckets)

    for station in tracked:
        for day, hour in station_buckets[station.id]:
            done_buckets += 1
            on_progress(done_buckets, total_buckets)
            # Looked up regardless of status (not just the 200 case): a bucket
            # that previously 404'd still has a row under the unique key
            # (eva, plan_date, plan_hour), and a caller that re-covers the same
            # window on purpose — as the collect orchestrator does, so an
            # overlap never leaves a gap — must update that row rather than
            # insert a second one, or the unique constraint raises.
            existing = session.execute(
                select(TtPlanFetch).where(
                    TtPlanFetch.eva == station.eva,
                    TtPlanFetch.plan_date == day,
                    TtPlanFetch.plan_hour == hour,
                )
            ).scalar_one_or_none()
            if not force and existing is not None and existing.http_status == 200:
                result.buckets_skipped += 1
                continue

            yymmdd = date_cls.fromisoformat(day).strftime("%y%m%d")
            resp = client.plan(station.eva, yymmdd, hour)
            fetch = existing or TtPlanFetch(eva=station.eva, plan_date=day, plan_hour=hour)
            fetch.fetched_at = int(time.time())
            fetch.http_status = resp.status
            fetch.content_sha256 = hashlib.sha256(resp.body).hexdigest() if resp.ok else None
            fetch.error = None
            fetch.n_stops = 0
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
                    stop.eva = station.eva
                by_run[stop.run_key][stop.stop_index] = stop

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
        now=int(time.time()),
    )
    counters = RunCounters()

    for run_key, stops_by_index in by_run.items():
        upsert_run(session, ctx, counters, run_key, list(stops_by_index.values()))

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
