"""The continuous collection cycle: plan -> poll -> finalize -> averages.

`dbrel collect` replaces "run nine commands by hand, then cron two of them" with
one process that runs a bounded number of hours or days and stops cleanly. It
is deliberately thin glue over the existing pipeline — every step below is a
function that already exists and is already tested; this module adds only the
scheduling, the station seed, and the one real bug the naive version of this
loop would hit (see `plan_window`).

Why not just call tt-poll's own --interval loop for everything: that loop only
ever calls run_tt_poll. It has no plan import, no finalize, no averages step,
and (docs/probe-findings.md) a rolling watermark that is unsafe at any cadence
slower than a couple of hours. `collect` is the one that is safe to leave
running for a week.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dbrel.config import (
    COLLECT_MAX_FAILED_CYCLES,
    COLLECT_OVERLAP_S,
    RAIL_EVA_RANGE,
    TT_PLAN_LEAD_HOURS,
    TT_SWEEP_LOCK,
    TZ,
)
from dbrel.models import BASIS_REAL, Station, TtPlanFetch

Progress = Callable[[str], None]


# --------------------------------------------------------------------------
# Duration parsing — "4h", "7d", "1h30m", "90", all as integer seconds.
# --------------------------------------------------------------------------

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(text: str) -> int:
    """Parse a human duration like '4h', '7d', '1h30m', '90s' into seconds.

    A bare integer is seconds. Case-insensitive, whitespace-tolerant, rejects
    negatives, floats and unknown units. This is the only place a human types
    a duration on the command line, so it errs on being strict rather than
    guessing.
    """
    raw = text.strip().lower()
    if not raw:
        raise ValueError(f"empty duration: {text!r}")
    if raw.isdigit():
        return int(raw)

    total = 0
    i = 0
    found_any = False
    while i < len(raw):
        start = i
        while i < len(raw) and raw[i].isdigit():
            i += 1
        if i == start:
            raise ValueError(f"invalid duration: {text!r}")
        number = int(raw[start:i])
        if i >= len(raw) or raw[i] not in _UNIT_SECONDS:
            raise ValueError(f"invalid duration: {text!r} (unknown unit)")
        unit = raw[i]
        i += 1
        total += number * _UNIT_SECONDS[unit]
        found_any = True
    if not found_any:
        raise ValueError(f"invalid duration: {text!r}")
    return total


def format_duration(seconds: int) -> str:
    """The inverse, roughly: 14400 -> '4h', 5400 -> '1h30m'. For echoing back."""
    if seconds <= 0:
        return "0s"
    parts = []
    remaining = seconds
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if remaining >= size:
            count, remaining = divmod(remaining, size)
            parts.append(f"{count}{unit}")
    return "".join(parts) or "0s"


# --------------------------------------------------------------------------
# Hour-bucket computation for /plan — the fix for the watermark trap.
#
# _pending_hours (timetables/plan_import.py) clamps its own lookback to two
# hours, so a caller on any slower cadence must compute explicit buckets
# itself and pass them via import_plan(date=, hours=) instead of trusting the
# rolling default. Walking must be done in UTC and converted to local per
# instant -- local-time arithmetic drifts across a DST transition, either
# skipping or doubling an hour that /plan buckets by wall-clock HH.
# --------------------------------------------------------------------------


def hour_buckets(start: datetime, end: datetime) -> list[tuple[str, int]]:
    """Every (YYYY-MM-DD, hour) bucket /plan needs to cover [start, end], local time.

    start and end must be timezone-aware. Deduplicated and in ascending order.
    Iterates in UTC (never local-time '+1h' steps) so a spring-forward hour is
    never requested and a fall-back hour is requested exactly once, matching
    what /plan actually has one bucket for.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("hour_buckets requires timezone-aware datetimes")
    # Truncate to the hour in UTC, then walk forward in fixed 1h UTC steps --
    # never in local time, where '+1h' is wall-clock arithmetic that drifts
    # across a DST transition.
    cursor = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end_utc = end.astimezone(timezone.utc)

    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    while cursor <= end_utc:
        local = cursor.astimezone(TZ)
        key = (local.date().isoformat(), local.hour)
        if key not in seen:
            seen.add(key)
            out.append(key)
        cursor += timedelta(hours=1)
    return out


def buckets_by_date(buckets: list[tuple[str, int]]) -> list[tuple[str, list[int]]]:
    """Group (date, hour) pairs into the (date, [hours]) shape import_plan takes.

    Dates ascending, hours ascending within a date -- import_plan writes
    station.last_plan_hour as it goes, so the later date must be applied last
    or the rolling-mode watermark (used by tt-import-plan with no --date) would
    be pushed backwards for anyone still relying on it.
    """
    by_date: dict[str, list[int]] = {}
    for day, hour in buckets:
        by_date.setdefault(day, []).append(hour)
    return [(day, sorted(set(hours))) for day, hours in sorted(by_date.items())]


def plan_window(
    previous_at: datetime | None,
    now: datetime,
    *,
    lead_hours: int = TT_PLAN_LEAD_HOURS,
    max_lookback_s: int,
    overlap_s: int = COLLECT_OVERLAP_S,
) -> tuple[datetime, datetime]:
    """The [start, end] a cycle's /plan import must cover, in local time.

    end is always now + lead_hours, matching what tt-import-plan already
    fetches on a manual run. start reaches back to the previous cycle minus a
    safety overlap, clamped to max_lookback_s -- reaching further than that
    buys nothing (§24: /plan purges past hours of today at ~14h) and costs a
    live, always-404 API call per station per hour every single cycle,
    because a 404 is retried forever while a 200 is not.
    """
    floor = now - timedelta(seconds=max_lookback_s)
    if previous_at is None:
        start = floor
    else:
        start = max(previous_at - timedelta(seconds=overlap_s), floor)
    end = now + timedelta(hours=lead_hours)
    return start, end


# --------------------------------------------------------------------------
# Station seed -- applying the checked-in Ruhrgebiet corridor with zero
# /station/ calls, so a reset never has to re-risk the §14/§22 name traps.
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SeedStation:
    name: str
    eva: int
    ds100: str | None
    natural_key: str | None


@dataclass(slots=True)
class SeedResult:
    applied: list[str] = field(default_factory=list)
    tracked: int = 0
    missing: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def load_station_seed(path: Path) -> list[SeedStation]:
    """Read the TOML station seed. See config/ruhr_stations.toml for the format."""
    with open(path, "rb") as f:
        doc = tomllib.load(f)
    out = []
    for entry in doc.get("stations", []):
        out.append(
            SeedStation(
                name=entry["name"],
                eva=int(entry["eva"]),
                ds100=entry.get("ds100"),
                natural_key=entry.get("natural_key"),
            )
        )
    return out


def apply_station_seed(
    session: Session,
    seed: list[SeedStation],
    *,
    priority: int = 1,
    progress: Progress = lambda msg: None,
) -> SeedResult:
    """Resolve each seed entry against the imported GTFS stations and track it.

    Matches by natural_key first (stable across a feed rebuild), then by exact
    name if natural_key misses or was not recorded and exactly one station has
    that name. Never queries /station/: the eva was already verified once by a
    human (docs/probe-findings.md §22), and re-deriving it from a name search
    after a reset is exactly the risk this file exists to avoid. The eva range
    check (RAIL_EVA_RANGE) and tt-verify-stations's behavioural ppth check
    afterwards remain the guards against a transposed number.
    """
    from dbrel.timetables.stations import track_stations

    result = SeedResult()
    to_track: list[int] = []

    for entry in seed:
        if entry.eva not in RAIL_EVA_RANGE:
            result.rejected.append(f"{entry.name}: eva {entry.eva} outside RAIL_EVA_RANGE")
            continue

        station = None
        if entry.natural_key:
            station = session.execute(
                select(Station).where(Station.natural_key == entry.natural_key)
            ).scalar_one_or_none()
        if station is None:
            matches = (
                session.execute(select(Station).where(Station.name == entry.name)).scalars().all()
            )
            if len(matches) == 1:
                station = matches[0]
            elif len(matches) > 1:
                result.ambiguous.append(f"{entry.name}: {len(matches)} stations share this name")
                continue

        if station is None:
            result.missing.append(entry.name)
            continue

        if station.eva is not None and station.eva != entry.eva:
            result.conflicts.append(
                f"{entry.name}: station already has eva={station.eva}, seed says {entry.eva}"
            )
            continue

        station.eva = entry.eva
        station.ds100 = station.ds100 or entry.ds100
        station.eva_source = "seed"
        result.applied.append(entry.name)
        to_track.append(station.id)

    session.flush()
    result.tracked = track_stations(session, to_track, priority=priority)

    for name in result.applied:
        progress(f"  seeded {name}")
    for name in result.missing:
        progress(f"  ! not found after import-static: {name}")
    for msg in result.ambiguous:
        progress(f"  ! ambiguous: {msg}")
    for msg in result.conflicts:
        progress(f"  ! conflict, not overwritten: {msg}")
    for msg in result.rejected:
        progress(f"  ! rejected: {msg}")
    return result


# --------------------------------------------------------------------------
# Single-instance lock. TT_SWEEP_LOCK was declared in config.py and never
# used anywhere; this is its first use. Modelled on the flock pattern already
# in timetables/ratelimit.py, but non-blocking: a second `collect` must fail
# fast with the holder's pid, not queue up and silently double the request
# rate the first instance is already budgeting for.
# --------------------------------------------------------------------------


class AlreadyRunning(RuntimeError):
    """Another `dbrel collect` (or similar) holds the sweep lock."""


@contextmanager
def exclusive_lock(path: Path = TT_SWEEP_LOCK, *, label: str = "collect") -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raw = os.read(fd, 4096).decode("utf-8", "replace")
            try:
                holder = json.loads(raw)
                detail = f"pid {holder.get('pid')} ({holder.get('label')}, started {holder.get('started_at')})"
            except json.JSONDecodeError:
                detail = "an unknown process"
            raise AlreadyRunning(f"{path} is held by {detail}") from None

        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        payload = json.dumps(
            {"pid": os.getpid(), "label": label, "started_at": datetime.now(TZ).isoformat()}
        ).encode("utf-8")
        os.write(fd, payload)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# --------------------------------------------------------------------------
# Cold start -- each step guarded by its own idempotent check, so a database
# reset (or a cold start interrupted partway through) always converges rather
# than needing a single all-or-nothing "is this fresh" flag.
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PhaseResult:
    name: str
    ok: bool
    summary: str
    error: str | None = None
    elapsed_s: float = 0.0


def _run_phase(name: str, fn: Callable[[], str]) -> PhaseResult:
    started = time.monotonic()
    try:
        summary = fn()
        return PhaseResult(name=name, ok=True, summary=summary, elapsed_s=time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see run_cycle docstring
        return PhaseResult(
            name=name, ok=False, summary="", error=repr(exc)[:300], elapsed_s=time.monotonic() - started
        )


def needs_cold_start(session: Session, *, expected_stations: int) -> bool:
    """True until the tracked set is fully seeded. The condition that actually
    matters -- true after reset-db, true after a half-finished first run,
    false otherwise -- rather than a boolean flag that can drift from reality.
    """
    tracked = session.scalar(select(func.count()).select_from(Station).where(Station.tracked == 1))
    return (tracked or 0) < expected_stations


def run_cold_start(
    *,
    stations_file: Path,
    priority: int,
    progress: Progress = lambda msg: None,
) -> list[PhaseResult]:
    """init-db -> import-static -> build-aliases -> station seed -> verify -> the
    one-off full-day /plan backfill (docs/probe-findings.md §24).
    """
    from dbrel.db import create_all, session_scope
    from dbrel.gtfs.download import fetch_static
    from dbrel.gtfs.static_import import import_feed
    from dbrel.search_index import index_stations
    from dbrel.timetables.client import TimetablesClient
    from dbrel.timetables.plan_import import import_plan
    from dbrel.timetables.stations import verify_station

    phases: list[PhaseResult] = []

    def phase(name: str, fn: Callable[[], str]) -> None:
        result = _run_phase(name, fn)
        phases.append(result)
        progress(f"  [{name}] {result.summary or result.error}")

    def do_schema() -> str:
        create_all()
        return "schema ready"

    phase("init-db", do_schema)

    def do_static() -> str:
        lines = []
        with session_scope() as session:
            for kind in ("fv", "rv"):
                download = fetch_static(kind)
                stats = import_feed(
                    session,
                    kind=kind,
                    url=download.url,
                    content=download.content,
                    sha256=download.sha256,
                    etag=download.etag,
                    last_modified=download.last_modified,
                    progress=lambda m: None,
                )
                if stats.skipped_reason:
                    lines.append(f"{kind}: already imported")
                else:
                    lines.append(f"{kind}: +{stats.stations} stations")
        return ", ".join(lines)

    phase("import-static", do_static)

    def do_seed() -> str:
        seed = load_station_seed(stations_file)
        with session_scope() as session:
            result = apply_station_seed(session, seed, priority=priority, progress=progress)
        if not result.applied:
            raise RuntimeError(
                f"none of {len(seed)} seed stations resolved against the imported GTFS data"
            )
        return f"{len(result.applied)}/{len(seed)} tracked"

    phase("station-seed", do_seed)

    def do_aliases() -> str:
        with session_scope() as session:
            result = index_stations(session, progress=lambda m: None)
        return f"{result.aliases_written} aliases"

    phase("build-aliases", do_aliases)

    def do_verify() -> str:
        lines = []
        with TimetablesClient() as client, session_scope() as session:
            client.require_credentials()
            stations = session.execute(select(Station).where(Station.tracked == 1)).scalars().all()
            for station in stations:
                overlap, note = verify_station(session, client, station)
                if overlap is None:
                    lines.append(f"{station.name}: skipped ({note})")
                    continue
                station.verify_overlap = overlap
                station.verified_at = int(time.time())
                if overlap < 0.6:
                    lines.append(f"{station.name}: LOW overlap {overlap:.1%} ({note})")
        return f"{len(stations)} checked" + ("; " + "; ".join(lines) if lines else "")

    phase("tt-verify-stations", do_verify)

    def do_backfill() -> str:
        from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

        today = datetime.now(TZ).date().isoformat()
        with TimetablesClient() as client, session_scope() as session:
            client.require_credentials()
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeRemainingColumn(),
            ) as bar:
                task_id = bar.add_task("plan-backfill buckets", total=None)
                result = import_plan(
                    session,
                    client,
                    date=today,
                    hours=list(range(24)),
                    progress=bar.console.print,
                    on_progress=lambda done, total: bar.update(task_id, completed=done, total=total),
                )
        return (
            f"{result.buckets_fetched} fetched, {result.buckets_missing} missing/404, "
            f"{result.trips_created} trips"
        )

    phase("plan-backfill", do_backfill)

    return phases


# --------------------------------------------------------------------------
# One cycle: plan -> poll -> finalize -> averages. Each phase is its own
# session_scope, so a failure in one does not roll back work already
# committed by an earlier phase in the same cycle.
# --------------------------------------------------------------------------


@dataclass(slots=True)
class CycleResult:
    index: int
    started_at: datetime
    phases: list[PhaseResult] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.phases)


def previous_plan_attempt(session: Session, evas: list[int]) -> datetime | None:
    """When /plan was last attempted for the tracked set, or None if any
    tracked station has never been asked -- which widens the window to cover
    a newly-added station until it has caught up.

    Derived from tt_plan_fetch.fetched_at rather than a new table or file: it
    is written on every attempt including 404s, so it already is the "did we
    look" log, and unlike a process-start timestamp it does not advance when a
    cycle's plan phase fails outright -- so a broken cycle widens next time's
    window instead of silently narrowing it.
    """
    if not evas:
        return None
    per_station = (
        session.execute(
            select(TtPlanFetch.eva, func.max(TtPlanFetch.fetched_at))
            .where(TtPlanFetch.eva.in_(evas))
            .group_by(TtPlanFetch.eva)
        )
        .all()
    )
    seen = {eva for eva, _ in per_station}
    if seen != set(evas):
        return None  # a tracked eva has no history at all yet
    earliest = min(ts for _, ts in per_station)
    return datetime.fromtimestamp(earliest, TZ)


def run_cycle(
    *,
    index: int,
    now: datetime,
    lead_hours: int,
    max_lookback_s: int,
    dry_run: bool = False,
    progress: Progress = lambda msg: None,
) -> CycleResult:
    from dbrel.db import session_scope
    from dbrel.derive.delay_averages import run_delay_averages
    from dbrel.derive.finalize_tt import run_finalize_tt
    from dbrel.timetables.changes_poller import run_tt_poll
    from dbrel.timetables.client import TimetablesClient
    from dbrel.timetables.plan_import import import_plan

    started = time.monotonic()
    result = CycleResult(index=index, started_at=now)

    with session_scope() as session:
        evas = [
            eva
            for (eva,) in session.execute(
                select(Station.eva).where(Station.tracked == 1, Station.eva.isnot(None))
            ).all()
        ]
        previous_at = previous_plan_attempt(session, evas)

    start, end = plan_window(
        previous_at, now, lead_hours=lead_hours, max_lookback_s=max_lookback_s
    )
    date_groups = buckets_by_date(hour_buckets(start, end))

    def do_plan() -> str:
        totals = {"fetched": 0, "skipped": 0, "missing": 0, "trips": 0, "stops": 0}
        with TimetablesClient() as client, session_scope() as session:
            client.require_credentials()
            for day, hours in date_groups:
                r = import_plan(session, client, date=day, hours=hours, progress=lambda m: None)
                totals["fetched"] += r.buckets_fetched
                totals["skipped"] += r.buckets_skipped
                totals["missing"] += r.buckets_missing
                totals["trips"] += r.trips_created
                totals["stops"] += r.station_stops_written
        return (
            f"{start.strftime('%m-%d %H:%M')}..{end.strftime('%m-%d %H:%M')}: "
            f"{totals['fetched']} fetched, {totals['skipped']} skipped, "
            f"{totals['missing']} missing/404, {totals['trips']} trips, {totals['stops']} stops"
        )

    result.phases.append(_run_phase("plan", do_plan))
    progress(f"  plan      {result.phases[-1].summary or result.phases[-1].error}")

    def do_poll() -> str:
        with TimetablesClient() as client, session_scope() as session:
            client.require_credentials()
            r = run_tt_poll(session, client, sweep_index=index, dry_run=dry_run, progress=lambda m: None)
        return (
            f"{r.stations_polled} stations, {r.stops_seen} stops, {r.matched} matched, "
            f"{r.unmatched} unmatched, {r.stored} stored"
        )

    result.phases.append(_run_phase("poll", do_poll))
    progress(f"  poll      {result.phases[-1].summary or result.phases[-1].error}")

    def do_finalize() -> str:
        with session_scope() as session:
            r = run_finalize_tt(session, progress=lambda m: None)
        return f"{r.trips_finalized}/{r.trips_considered} trips, {r.gaps_written} gaps filled"

    result.phases.append(_run_phase("finalize", do_finalize))
    progress(f"  finalize  {result.phases[-1].summary or result.phases[-1].error}")

    def do_averages() -> str:
        with session_scope() as session:
            r = run_delay_averages(session, basis=BASIS_REAL, progress=lambda m: None)
        return f"{r.pairs_written} pairs written, {r.samples_read} samples"

    result.phases.append(_run_phase("averages", do_averages))
    progress(f"  averages  {result.phases[-1].summary or result.phases[-1].error}")

    result.elapsed_s = time.monotonic() - started
    return result


# --------------------------------------------------------------------------
# The bounded run: cold start, then cycles until the deadline or Ctrl-C.
# --------------------------------------------------------------------------


@dataclass(slots=True)
class StopFlag:
    requested: bool = False
    hard: bool = False


@dataclass(slots=True)
class CollectResult:
    cold_start: list[PhaseResult] = field(default_factory=list)
    cycles: list[CycleResult] = field(default_factory=list)
    interrupted: bool = False
    aborted_reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(TZ))
    elapsed_s: float = 0.0


def run_collect(
    *,
    every_s: int,
    for_s: int,
    lead_hours: int = TT_PLAN_LEAD_HOURS,
    max_lookback_s: int,
    stations_file: Path,
    cold_start: bool = True,
    once: bool = False,
    dry_run: bool = False,
    priority: int = 1,
    progress: Progress = lambda msg: None,
) -> CollectResult:
    """Run cold-start-if-needed, then cycles every `every_s` seconds for `for_s`
    seconds (0 = until interrupted), or exactly one cycle if `once`.

    Ctrl-C: the first SIGINT sets a flag that is checked between phases and in
    every sleep slice, so the current phase finishes and commits (each phase
    owns its own session_scope) before the loop exits with a clean summary. A
    second SIGINT restores the default handler, so it raises immediately --
    a hard abort is always two keystrokes away.
    """
    from dbrel.db import create_all, session_scope

    overall_started = time.monotonic()
    out = CollectResult()
    stop = StopFlag()

    def handler(signum, frame):  # noqa: ANN001, ARG001
        if stop.requested:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            return
        stop.requested = True
        progress("\n  interrupt received -- finishing the current phase (Ctrl-C again to abort now)")

    previous_int = signal.signal(signal.SIGINT, handler)
    previous_term = signal.signal(signal.SIGTERM, handler)

    try:
        with exclusive_lock(TT_SWEEP_LOCK, label="collect"):
            if cold_start:
                # create_all() is a no-op against an existing schema, but on a
                # database that does not exist at all yet, needs_cold_start's own
                # query against `station` would otherwise fail before cold start
                # has had a chance to create the table.
                create_all()
                with session_scope() as session:
                    seed = load_station_seed(stations_file)
                    do_it = needs_cold_start(session, expected_stations=len(seed))
                if do_it:
                    out.cold_start = run_cold_start(
                        stations_file=stations_file, priority=priority, progress=progress
                    )
                    if not all(p.ok for p in out.cold_start):
                        out.aborted_reason = "cold start failed: " + "; ".join(
                            f"{p.name}: {p.error}" for p in out.cold_start if p.error is not None
                        )
                        return out

            deadline = time.monotonic() + for_s if for_s else None
            consecutive_failures = 0
            index = 0
            while True:
                now = datetime.now(TZ)
                progress(f"\n=== cycle {index}  {now.strftime('%Y-%m-%d %H:%M:%S')}")
                cycle_started = time.monotonic()
                cycle = run_cycle(
                    index=index,
                    now=now,
                    lead_hours=lead_hours,
                    max_lookback_s=max_lookback_s,
                    dry_run=dry_run,
                    progress=progress,
                )
                out.cycles.append(cycle)
                progress(f"  cycle {'ok' if cycle.ok else 'HAD FAILURES'} in {cycle.elapsed_s:.1f}s")

                consecutive_failures = 0 if cycle.ok else consecutive_failures + 1
                if consecutive_failures >= COLLECT_MAX_FAILED_CYCLES:
                    out.aborted_reason = (
                        f"{consecutive_failures} consecutive failed cycles -- stopping rather than "
                        "spending a week doing nothing"
                    )
                    break

                index += 1
                if once or stop.requested:
                    break
                if deadline and time.monotonic() >= deadline:
                    break

                wait = every_s - (time.monotonic() - cycle_started)
                if wait > 0:
                    if deadline and time.monotonic() + wait > deadline:
                        break
                    progress(f"  next cycle in {format_duration(int(wait))}")
                    slept = 0.0
                    while slept < wait and not stop.requested:
                        step = min(1.0, wait - slept)
                        time.sleep(step)
                        slept += step
                elif deadline and time.monotonic() >= deadline:
                    break
    except KeyboardInterrupt:
        out.interrupted = True
    else:
        out.interrupted = stop.requested
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        out.elapsed_s = time.monotonic() - overall_started

    return out
