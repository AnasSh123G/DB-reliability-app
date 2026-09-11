"""Command line interface. Every command is one-shot: it runs, reports, exits."""

from __future__ import annotations

from datetime import datetime

import typer
from sqlalchemy import func, select, text

from dbrel.config import DB_PATH, STATIC_FEEDS
from dbrel.db import create_all, session_scope
from dbrel.gtfs.download import fetch_static
from dbrel.timetables.parser import parse_stations
from dbrel.gtfs.static_import import import_feed
from dbrel.models import (
    TRUSTED_QUALITIES,
    SearchAlias,
    ServiceLine,
    TrackedStation,
    TrainConflict,
    TtMessage,
    TtObservation,
    TtPoll,
    TtUnmatched,
    FeedVersion,
    Route,
    RtObservation,
    RtPoll,
    RtUnmatched,
    Station,
    StationStop,
    Train,
    Trip,
    TripServiceDate,
    TripStop,
)

app = typer.Typer(add_completion=False, help="Local database of German train punctuality.")


def _require_schema(session) -> None:
    """Fail readably when there is no database yet.

    Read commands must not create the schema — pointing DBREL_DB at a typo should
    not leave 24 empty tables behind — so they check instead.
    """
    present = session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trip_stop'")
    ).first()
    if present is None:
        typer.echo(f"no schema at {DB_PATH} — run 'dbrel init-db' first")
        raise typer.Exit(1)


@app.command("init-db")
def init_db() -> None:
    """Create the schema."""
    create_all()
    typer.echo(f"schema ready at {DB_PATH}")


@app.command("import-static")
def import_static(
    feed: str = typer.Option("fv,rv", help="comma-separated feeds: fv, rv"),
    force: bool = typer.Option(False, help="re-import even if the content hash is known"),
    max_days: int | None = typer.Option(None, help="limit dated runs to N days from feed start"),
) -> None:
    """Download and import the scheduled timetable."""
    create_all()
    kinds = [k.strip() for k in feed.split(",") if k.strip()]
    for kind in kinds:
        if kind not in STATIC_FEEDS:
            raise typer.BadParameter(f"unknown feed {kind!r}; expected {sorted(STATIC_FEEDS)}")

    for kind in kinds:
        typer.echo(f"\n=== {kind}")
        download = fetch_static(kind)
        typer.echo(
            f"  {'cached' if download.from_cache else 'downloaded'} {download.size:,} bytes"
            f"  sha256={download.sha256[:12]}"
        )
        with session_scope() as session:
            stats = import_feed(
                session,
                kind=kind,
                url=download.url,
                content=download.content,
                sha256=download.sha256,
                etag=download.etag,
                last_modified=download.last_modified,
                force=force,
                max_days=max_days,
                progress=typer.echo,
            )
        if stats.skipped_reason:
            typer.echo(f"  skipped: {stats.skipped_reason}")
            continue
        typer.echo(
            f"  imported feed_version={stats.feed_version_id} in {stats.elapsed_s:.1f}s\n"
            f"    stations +{stats.stations:,}   routes {stats.routes:,}   trains +{stats.trains:,}\n"
            f"    station_stops {stats.station_stops:,}   service_dates {stats.service_dates:,}\n"
            f"    dated runs {stats.trips:,}   (gtfs trips read {stats.gtfs_trips_read:,},"
            f" non-rail skipped {stats.non_rail_trips:,}, duplicate runs {stats.duplicate_runs:,})"
        )


@app.command("poll")
def poll(
    dry_run: bool = typer.Option(False, help="report match rates without writing"),
) -> None:
    """Fetch the realtime feed once and record what changed."""
    from dbrel.realtime.poller import run_poll

    with session_scope() as session:
        result = run_poll(session, dry_run=dry_run, progress=typer.echo)
    typer.echo(result.summary())


@app.command("finalize")
def finalize(
    date: str | None = typer.Option(None, help="service date YYYY-MM-DD (default: all closed)"),
    force: bool = typer.Option(False, help="re-derive trips that were already finalized"),
) -> None:
    """Derive actual times from the recorded observations."""
    from dbrel.derive.finalize import run_finalize

    with session_scope() as session:
        result = run_finalize(session, service_date=date, force=force, progress=typer.echo)
    typer.echo(result.summary())


@app.command("stats")
def stats() -> None:
    """Show what the database currently holds."""
    with session_scope() as session:
        _require_schema(session)
        typer.echo("tables")
        for label, model in [
            ("feed_version", FeedVersion),
            ("route", Route),
            ("station", Station),
            ("train", Train),
            ("station_stop", StationStop),
            ("trip_service_date", TripServiceDate),
            ("trip", Trip),
            ("trip_stop", TripStop),
            ("rt_poll", RtPoll),
            ("rt_observation", RtObservation),
            ("rt_unmatched", RtUnmatched),
            ("service_line", ServiceLine),
            ("search_alias", SearchAlias),
            ("tracked_station", TrackedStation),
            ("tt_poll", TtPoll),
            ("tt_observation", TtObservation),
            ("tt_message", TtMessage),
            ("tt_unmatched", TtUnmatched),
            ("train_conflict", TrainConflict),
        ]:
            count = session.scalar(select(func.count()).select_from(model))
            typer.echo(f"  {label:20s} {count:>12,}")

        by_source = session.execute(
            select(
                Train.source,
                func.count(),
                func.count(Train.train_number),
            ).group_by(Train.source)
        ).all()
        if by_source:
            typer.echo("\ntrains by source")
            for src, total, numbered in by_source:
                typer.echo(
                    f"  {src:20s} {total:>12,}  with train_number: "
                    f"{numbered:,} ({numbered / total:.0%})"
                )

        versions = session.execute(
            select(FeedVersion).order_by(FeedVersion.id.desc()).limit(5)
        ).scalars().all()
        if versions:
            typer.echo("\nfeed versions (newest first)")
            for v in versions:
                imported = (
                    datetime.fromtimestamp(v.imported_at).strftime("%Y-%m-%d %H:%M")
                    if v.imported_at
                    else "-"
                )
                typer.echo(
                    f"  {v.id:>3} {v.kind:3s} {v.status:11s} {imported}"
                    f"  {v.feed_start_date}..{v.feed_end_date}  trips={v.n_trips or 0:,}"
                )

        polls = session.execute(
            select(RtPoll).order_by(RtPoll.id.desc()).limit(8)
        ).scalars().all()
        if polls:
            typer.echo("\nrecent polls (newest first)")
            for p in polls:
                when = datetime.fromtimestamp(p.fetched_at).strftime("%Y-%m-%d %H:%M:%S")
                if p.error:
                    typer.echo(f"  {when}  ERROR {p.error[:60]}")
                else:
                    matched = p.n_matched or 0
                    total = (p.n_matched or 0) + (p.n_unmatched or 0)
                    rate = matched / total if total else 0.0
                    typer.echo(
                        f"  {when}  updates={p.n_trip_updates or 0:>6,}"
                        f"  matched={matched:>5,} ({rate:.0%})"
                        f"  changed={p.n_changed or 0:>6,}"
                        f"  {(p.bytes or 0) / 1e6:.0f}MB"
                        f"  fetch={p.fetch_ms or 0}ms parse={p.parse_ms or 0}ms store={p.store_ms or 0}ms"
                    )

        quality = session.execute(
            select(TripStop.quality, func.count())
            .group_by(TripStop.quality)
            .order_by(func.count().desc())
        ).all()
        if quality:
            typer.echo("\ntrip_stop quality")
            total = sum(c for _, c in quality)
            for grade, count in quality:
                typer.echo(f"  {grade:22s} {count:>10,}  {count / total:6.1%}")

        # Only trusted grades: a PREDICTION_ONLY value is a guess nobody confirmed,
        # and averaging it in would quietly flatter the numbers.
        trusted = (
            TripStop.dep_delay_s.isnot(None),
            TripStop.quality.in_(TRUSTED_QUALITIES),
        )
        delays = session.execute(
            select(
                func.count(),
                func.avg(TripStop.dep_delay_s),
                func.max(TripStop.dep_delay_s),
            ).where(*trusted)
        ).one()
        if delays[0]:
            typer.echo(
                f"\ndeparture delay over {delays[0]:,} stops"
                f" (quality {'/'.join(TRUSTED_QUALITIES)}):"
                f" mean={delays[1] / 60:.1f}min  max={delays[2] / 60:.0f}min"
            )
            on_time = session.scalar(
                select(func.count())
                .select_from(TripStop)
                .where(*trusted, TripStop.dep_delay_s < 360)
            )
            typer.echo(f"  within 6 min (DB's punctuality threshold): {on_time / delays[0]:.1%}")

        window = session.execute(
            select(func.min(Trip.service_date), func.max(Trip.service_date))
        ).one()
        if window[0]:
            typer.echo(f"\nscheduled runs span {window[0]} .. {window[1]}")
        typer.echo(f"\ndatabase: {DB_PATH}")


@app.command("unmatched")
def unmatched(limit: int = typer.Option(20, help="rows to show")) -> None:
    """Show feed entries that resolved to no known run.

    The Timetables half is the one that matters operationally: a rising count
    means the /plan roller has fallen behind, and because /plan serves only today
    and tomorrow, that planned data is gone for good.
    """
    with session_scope() as session:
        _require_schema(session)
        tt_total = session.scalar(select(func.count()).select_from(TtUnmatched))
        typer.echo(f"{tt_total:,} unmatched Timetables stop events")
        rows = session.execute(
            select(TtUnmatched).order_by(TtUnmatched.n_polls.desc()).limit(limit)
        ).scalars().all()
        for r in rows:
            label = f"{r.category or '?'} {r.number or '?'}"
            typer.echo(
                f"  {r.service_date}  {label:<12} eva={r.eva}  stop={r.stop_index:>3}"
                f"  seen in {r.n_polls} polls"
            )

        rt_total = session.scalar(select(func.count()).select_from(RtUnmatched))
        typer.echo(f"\n{rt_total:,} unmatched realtime trips")
        rows = session.execute(
            select(RtUnmatched).order_by(RtUnmatched.n_polls.desc()).limit(limit)
        ).scalars().all()
        for r in rows:
            typer.echo(
                f"  {r.service_date}  trip={r.gtfs_trip_id:>10}  stops={r.n_stop_updates or 0:>3}"
                f"  seen in {r.n_polls} polls"
            )


def main() -> None:
    app()


if __name__ == "__main__":
    main()


# --- DB Timetables (IRIS-TTS) ---------------------------------------------


@app.command("tt-check")
def tt_check(
    pattern: str = typer.Option("BLS", help="station pattern to probe with"),
    burst: int = typer.Option(0, help="fire N requests as fast as allowed"),
) -> None:
    """Verify the Timetables credentials and report the rate limit.

    Tries the header combinations separately: one bad header and two bad headers
    both return 401, so only this tells you which half is wrong.
    """
    from dbrel.timetables.client import TimetablesClient, credential_report

    for line in credential_report():
        typer.echo("  " + line)

    client = TimetablesClient()
    typer.echo(f"\n  {'headers sent':<28} {'status':<8} body[:80]")
    typer.echo(f"  {'-' * 28} {'-' * 8} {'-' * 80}")
    working = None
    for label, flags in (
        ("DB-Client-Id + DB-Api-Key", dict(with_id=True, with_key=True)),
        ("DB-Api-Key only", dict(with_id=False, with_key=True)),
        ("DB-Client-Id only", dict(with_id=True, with_key=False)),
        ("neither", dict(with_id=False, with_key=False)),
    ):
        resp = client.get(f"/station/{pattern}", **flags)
        snippet = resp.body[:80].decode("utf-8", "replace").replace("\n", " ")
        typer.echo(f"  {label:<28} {resp.status:<8} {snippet}")
        if resp.ok and working is None:
            working = label
            for key, value in resp.rate_limit.items():
                typer.echo(f"      {key}: {value}")
    typer.echo(f"\n  authenticates with: {working or 'NOTHING - check the subscription'}")

    if burst and working:
        codes: dict[int, int] = {}
        started = datetime.now()
        for _ in range(burst):
            resp = client.get(f"/station/{pattern}")
            codes[resp.status] = codes.get(resp.status, 0) + 1
        elapsed = (datetime.now() - started).total_seconds()
        typer.echo(f"  burst: {burst} requests in {elapsed:.1f}s -> {codes}")
    client.close()


@app.command("tt-rank-stations")
def tt_rank_stations(
    near: str = typer.Option(None, help="rank only stations within --km of this one"),
    km: float = typer.Option(25.0, help="radius when --near is given"),
    limit: int = typer.Option(40),
) -> None:
    """Rank stations by how much traffic they would bring into the tracked set.

    Pure SQL over the GTFS pattern table; no API calls. The cumulative column is
    the point: it lets the station count be chosen against evidence.
    """
    with session_scope() as session:
        where = ""
        params: dict = {}
        if near:
            anchor = session.execute(
                select(Station.lat, Station.lon).where(Station.name.like(f"%{near}%")).limit(1)
            ).first()
            if anchor is None:
                raise typer.BadParameter(f"no station matching {near!r}")
            where = (
                "WHERE 111.0 * sqrt((s.lat - :lat)*(s.lat - :lat) "
                "+ (s.lon - :lon)*(s.lon - :lon)*0.39) <= :km"
            )
            params = {"lat": anchor[0], "lon": anchor[1], "km": km}

        rows = session.execute(
            text(
                f"""
                SELECT s.id, s.name, COUNT(DISTINCT ss.train_id) AS n_trains,
                       COUNT(*) AS n_stops
                FROM station s JOIN station_stop ss ON ss.station_id = s.id
                {where}
                GROUP BY s.id ORDER BY n_trains DESC LIMIT :limit
                """
            ),
            {**params, "limit": limit},
        ).all()

    typer.echo("station_id\tname\tn_trains\tn_stops")
    for station_id, name, n_trains, n_stops in rows:
        typer.echo(f"{station_id}\t{name}\t{n_trains}\t{n_stops}")


@app.command("tt-map-stations")
def tt_map_stations(
    station_ids: str = typer.Option(None, help="comma-separated station ids to track"),
    manual: str = typer.Option(None, help="station_id=eva pairs the resolver cannot get"),
    priority: int = typer.Option(1, help="1 = poll every sweep"),
) -> None:
    """Resolve each station to an IRIS eva and add it to the tracked set.

    Only accepted matches are saved: the eva must be a railway eva and the name
    must loosely match. A wrong eva is invisible downstream, so nothing marginal
    is auto-accepted — unresolved stations are reported with the queries that
    were tried, for a human to settle with --manual.
    """
    from dbrel.timetables.client import TimetablesClient
    from dbrel.timetables.stations import map_stations, track_stations

    ids = [int(x) for x in (station_ids or "").split(",") if x.strip()]
    pairs = {}
    for item in (manual or "").split(","):
        if "=" in item:
            sid, eva = item.split("=", 1)
            pairs[int(sid)] = int(eva)

    with TimetablesClient() as client, session_scope() as session:
        client.require_credentials()
        accepted = map_stations(session, client, ids, progress=typer.echo) if ids else {}
        for sid, eva in pairs.items():
            station = session.get(Station, sid)
            if station is None:
                typer.echo(f"  ! no station {sid}")
                continue
            resp = client.get(f"/station/{eva}")
            names = parse_stations(resp.body) if resp.ok else []
            station.eva = eva
            station.ds100 = names[0]["ds100"] if names else None
            station.eva_source = "manual"
            station.eva_confidence = 0.0
            typer.echo(
                f"  {station.name}: eva={eva} ds100={station.ds100} "
                f"(manual; IRIS calls it {names[0]['name']!r})"
                if names
                else f"  {station.name}: eva={eva} (manual, unverified)"
            )
        session.flush()
        added = track_stations(session, list(accepted) + list(pairs), priority=priority)
    typer.echo(f"\n  resolved {len(accepted)}/{len(ids)} + {len(pairs)} manual, {added} newly tracked")


@app.command("tt-verify-stations")
def tt_verify_stations(min_overlap: float = typer.Option(0.6)) -> None:
    """Check each tracked station behaviourally: do IRIS's neighbours match GTFS's?"""
    from dbrel.timetables.client import TimetablesClient
    from dbrel.timetables.stations import verify_station

    with TimetablesClient() as client, session_scope() as session:
        client.require_credentials()
        rows = session.execute(
            select(TrackedStation, Station).join(Station, Station.id == TrackedStation.station_id)
        ).all()
        for tracked, station in rows:
            overlap, note = verify_station(session, client, station)
            if overlap is None:
                typer.echo(f"  {station.name:<26} SKIPPED  {note}")
                continue
            tracked.verify_overlap = overlap
            tracked.verified_at = int(datetime.now().timestamp())
            verdict = "ok" if overlap >= min_overlap else "LOW"
            typer.echo(f"  {station.name:<26} {overlap:6.1%} {verdict:<4} {note}")


@app.command("tt-import-plan")
def tt_import_plan(
    date: str = typer.Option(None, help="YYYY-MM-DD; default rolls forward from the high-water mark"),
    hours: str = typer.Option(None, help="e.g. 5-22 or 8,9,17 (only with --date)"),
    lead_hours: int = typer.Option(3),
    force: bool = typer.Option(False, help="re-fetch buckets already recorded"),
) -> None:
    """Import planned timetables for the tracked stations.

    /plan serves only today and tomorrow, so a bucket missed here is lost.
    """
    from dbrel.timetables.client import TimetablesClient
    from dbrel.timetables.plan_import import import_plan

    hour_list = None
    if hours:
        if "-" in hours:
            lo, hi = hours.split("-")
            hour_list = list(range(int(lo), int(hi) + 1))
        else:
            hour_list = [int(h) for h in hours.split(",")]

    with TimetablesClient() as client, session_scope() as session:
        client.require_credentials()
        result = import_plan(
            session,
            client,
            lead_hours=lead_hours,
            date=date,
            hours=hour_list,
            force=force,
            progress=typer.echo,
        )
    typer.echo(
        f"  buckets: {result.buckets_fetched} fetched, {result.buckets_skipped} skipped, "
        f"{result.buckets_missing} missing/404"
    )
    typer.echo(
        f"  trains {result.trains_created} new, lines {result.lines_created}, "
        f"trips {result.trips_created}, station_stops {result.station_stops_written}"
    )
    if result.conflicts:
        typer.echo(f"  {result.conflicts} identity conflicts parked in train_conflict")
    if result.dst_fixups:
        typer.echo(f"  {result.dst_fixups} DST monotonicity fixups")
    for err in result.errors[:5]:
        typer.echo(f"  ! {err}")


@app.command("tt-poll")
def tt_poll(
    interval: int = typer.Option(0, help="seconds between sweeps; 0 = one sweep and exit"),
    minutes: int = typer.Option(0, help="stop after this long (with --interval)"),
    dry_run: bool = typer.Option(False),
) -> None:
    """Poll /fchg for the tracked stations, recording events that have happened."""
    import time as _time

    from dbrel.timetables.changes_poller import run_tt_poll
    from dbrel.timetables.client import TimetablesClient

    deadline = _time.monotonic() + minutes * 60 if minutes else None
    sweep = 0
    with TimetablesClient() as client:
        client.require_credentials()
        while True:
            sweep_started = _time.monotonic()
            with session_scope() as session:
                result = run_tt_poll(
                    session, client, sweep_index=sweep, dry_run=dry_run, progress=typer.echo
                )
            typer.echo(
                f"  sweep {sweep}: {result.stations_polled} stations, "
                f"{result.stops_seen} stops, {result.matched} matched, "
                f"{result.created} runs created, {result.unmatched} unmatched, "
                f"{result.pending} not yet due, {result.stored} stored, "
                f"{result.cancellations} cancellations, {result.elapsed_s:.1f}s"
            )
            for err in result.errors[:3]:
                typer.echo(f"  ! {err}")
            sweep += 1
            if not interval or (deadline and _time.monotonic() >= deadline):
                break
            wait = interval - (_time.monotonic() - sweep_started)
            if wait > 0:
                if deadline and _time.monotonic() + wait > deadline:
                    break
                _time.sleep(wait)


@app.command("tt-finalize")
def tt_finalize(
    service_date: str = typer.Option(None),
    force: bool = typer.Option(False),
) -> None:
    """Derive actual times for Timetables runs (rule_version 2)."""
    from dbrel.derive.finalize_tt import run_finalize_tt

    with session_scope() as session:
        result = run_finalize_tt(
            session, service_date=service_date, force=force, progress=typer.echo
        )
    typer.echo(
        f"  {result.trips_finalized}/{result.trips_considered} trips, "
        f"{result.stops_written} stops, {result.cancelled_stops} cancelled, "
        f"{result.elapsed_s:.1f}s"
    )
    for quality, count in sorted(result.by_quality.items(), key=lambda kv: -kv[1]):
        typer.echo(f"    {quality:<22} {count:>7,}")


@app.command("search")
def search(term: str = typer.Argument(...), limit: int = typer.Option(10)) -> None:
    """Find a train by number ('ICE 599', '4523') or a line ('RE 4', 'S1')."""
    from dbrel.identity import prefix_range, search_norm

    normalised = search_norm(term)
    lo, hi = prefix_range(normalised)
    with session_scope() as session:
        _require_schema(session)
        rows = session.execute(
            select(
                SearchAlias.alias_display,
                SearchAlias.kind,
                SearchAlias.weight,
                SearchAlias.train_id,
                SearchAlias.line_id,
            )
            .where(SearchAlias.alias_norm >= lo, SearchAlias.alias_norm < hi)
            .order_by(
                (SearchAlias.alias_norm == normalised).desc(),
                SearchAlias.weight.desc(),
            )
            .limit(limit)
        ).all()
        if not rows:
            typer.echo(f"no match for {term!r}")
            return
        for display, kind, weight, train_id, line_id in rows:
            if train_id:
                train = session.get(Train, train_id)
                typer.echo(
                    f"  TRAIN  {display:<14} #{train.train_number}  {train.category}  "
                    f"{train.origin_name} -> {train.dest_name}  "
                    f"(id={train.id}, {train.n_stops} stops, {train.n_runs_seen} runs)"
                )
            elif line_id:
                line = session.get(ServiceLine, line_id)
                n = session.execute(
                    select(func.count()).select_from(Train).where(Train.line_id == line_id)
                ).scalar_one()
                typer.echo(f"  LINE   {display:<14} {line.category}  {n} numbered trains (id={line.id})")


# --- reading the log ------------------------------------------------------
# TSV columns are the same on both commands and carry raw epochs alongside the
# formatted times, so a training script never has to parse the display output.
_TSV_STOP_COLUMNS = (
    "stop_sequence",
    "station_id",
    "station_name",
    "supposed_arrival_utc",
    "actual_arrival_utc",
    "arr_delay_s",
    "supposed_departure_utc",
    "actual_departure_utc",
    "dep_delay_s",
    "quality",
    "cancelled",
    "read_lag_s",
    "planned_platform",
    "changed_platform",
)


# One arrival or departure block: two clocks and a signed delay, 19 columns wide.
# Kept next to the row format below so the two cannot drift apart.
_TIME_BLOCK = f"{'sched':<5} {'act':<5} {'+/-':>5}  "


def _day_window(service_date: str) -> tuple[int, int]:
    """UTC bounds of one local calendar day.

    Converting the date to instants is what lets a station query seek
    ix_ts_station_dep. Filtering on trip.service_date instead reads every stop
    ever recorded at the station and discards almost all of it.
    """
    from datetime import date as date_cls
    from datetime import timedelta

    from dbrel.config import TZ

    day = date_cls.fromisoformat(service_date)
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def _clock(epoch: int | None) -> str:
    if epoch is None:
        return "--:--"
    from dbrel.config import TZ

    return datetime.fromtimestamp(epoch, TZ).strftime("%H:%M")


def _signed_minutes(seconds: int | None) -> str:
    if seconds is None:
        return "     "
    return f"{seconds / 60:+5.0f}"


def _resolve_train(session, term: str) -> int | None:
    from dbrel.identity import search_norm

    hit = session.execute(
        select(SearchAlias.train_id)
        .where(SearchAlias.alias_norm == search_norm(term), SearchAlias.train_id.isnot(None))
        .order_by(SearchAlias.weight.desc())
        .limit(1)
    ).scalar_one_or_none()
    if hit is not None:
        return hit
    if term.isdigit():
        return session.execute(
            select(Train.id).where(Train.train_number == int(term)).limit(1)
        ).scalar_one_or_none()
    return None


def _resolve_station(session, term: str) -> int | None:
    if term.isdigit():
        hit = session.execute(
            select(Station.id).where(Station.eva == int(term)).limit(1)
        ).scalar_one_or_none()
        if hit is not None:
            return hit
    # Anchored, so ix_station_name is usable. A leading wildcard would not be.
    return session.execute(
        select(Station.id).where(Station.name.like(f"{term}%")).order_by(Station.name).limit(1)
    ).scalar_one_or_none()


@app.command("show-train")
def show_train(
    train: str = typer.Argument(..., help="train number or name: 599, 'ICE 599'"),
    date: str = typer.Option(..., "--date", help="service date, YYYY-MM-DD"),
    fmt: str = typer.Option("table", "--format", help="table | tsv"),
) -> None:
    """Every stop of one run, scheduled against actual."""
    with session_scope() as session:
        _require_schema(session)
        train_id = _resolve_train(session, train)
        if train_id is None:
            typer.echo(f"no train matching {train!r}")
            raise typer.Exit(1)
        row = session.execute(
            select(Trip, Train)
            .join(Train, Train.id == Trip.train_id)
            .where(Trip.train_id == train_id, Trip.service_date == date)
        ).first()
        if row is None:
            typer.echo(f"no run of train id {train_id} on {date}")
            raise typer.Exit(1)
        trip, train_row = row
        stops = session.execute(
            select(TripStop, Station.name)
            .join(Station, Station.id == TripStop.station_id)
            .where(TripStop.trip_id == trip.id)
            .order_by(TripStop.stop_sequence)
        ).all()

        if fmt == "tsv":
            typer.echo("\t".join(_TSV_STOP_COLUMNS))
            for stop, station_name in stops:
                typer.echo(_stop_tsv(stop, station_name))
            return

        typer.echo(
            f"\n{train_row.display_name or train_row.long_name}  ·  {date}  ·  {trip.status}"
            f"  ·  {trip.n_stops_tracked or 0} of {trip.n_stops or 0} stops tracked"
        )
        if trip.final_delay_s is not None:
            typer.echo(f"final delay {trip.final_delay_s / 60:+.0f} min")
        typer.echo(f"route: {train_row.route_path or '—'}")
        typer.echo(f"\n seq  {'station':<27} {'arrival':<19}{'departure':<19}quality")
        typer.echo(f"      {'':<27} {_TIME_BLOCK}{_TIME_BLOCK}")
        for stop, station_name in stops:
            typer.echo(
                f" {stop.stop_sequence:>3}  {station_name[:27]:<27} "
                f"{_clock(stop.supposed_arrival_utc)} {_clock(stop.actual_arrival_utc)} "
                f"{_signed_minutes(stop.arr_delay_s)}  "
                f"{_clock(stop.supposed_departure_utc)} {_clock(stop.actual_departure_utc)} "
                f"{_signed_minutes(stop.dep_delay_s)}  {stop.quality}"
            )
        typer.echo("")


@app.command("show-station")
def show_station(
    station: str = typer.Argument(..., help="station name prefix or eva number"),
    date: str = typer.Option(..., "--date", help="local calendar date, YYYY-MM-DD"),
    fmt: str = typer.Option("table", "--format", help="table | tsv"),
    limit: int = typer.Option(200),
) -> None:
    """Everything that called at one station on one day."""
    from sqlalchemy import and_, or_

    with session_scope() as session:
        _require_schema(session)
        station_id = _resolve_station(session, station)
        if station_id is None:
            typer.echo(f"no station matching {station!r}")
            raise typer.Exit(1)
        station_row = session.get(Station, station_id)
        lo, hi = _day_window(date)
        ordering = func.coalesce(TripStop.supposed_departure_utc, TripStop.supposed_arrival_utc)
        rows = session.execute(
            select(TripStop, Train)
            .join(Trip, Trip.id == TripStop.trip_id)
            .join(Train, Train.id == Trip.train_id)
            .where(
                TripStop.station_id == station_id,
                or_(
                    and_(
                        TripStop.supposed_departure_utc >= lo,
                        TripStop.supposed_departure_utc < hi,
                    ),
                    and_(
                        TripStop.supposed_arrival_utc >= lo,
                        TripStop.supposed_arrival_utc < hi,
                    ),
                ),
            )
            .order_by(ordering)
            .limit(limit)
        ).all()

        if fmt == "tsv":
            typer.echo("train\ttrain_number\t" + "\t".join(_TSV_STOP_COLUMNS))
            for stop, train_row in rows:
                label = train_row.display_name or ""
                typer.echo(
                    f"{label}\t{train_row.train_number or ''}\t"
                    + _stop_tsv(stop, station_row.name)
                )
            return

        typer.echo(f"\n{station_row.name}  ·  {date}  ·  {len(rows)} calls")
        typer.echo(f"\n {'train':<15}  {'arrival':<19}{'departure':<19}quality")
        typer.echo(f" {'':<15}  {_TIME_BLOCK}{_TIME_BLOCK}")
        for stop, train_row in rows:
            typer.echo(
                f" {(train_row.display_name or '?')[:15]:<15}  "
                f"{_clock(stop.supposed_arrival_utc)} {_clock(stop.actual_arrival_utc)} "
                f"{_signed_minutes(stop.arr_delay_s)}  "
                f"{_clock(stop.supposed_departure_utc)} {_clock(stop.actual_departure_utc)} "
                f"{_signed_minutes(stop.dep_delay_s)}  {stop.quality}"
            )
        typer.echo("")


def _stop_tsv(stop, station_name: str) -> str:
    return "\t".join(
        "" if value is None else str(value)
        for value in (
            stop.stop_sequence,
            stop.station_id,
            station_name,
            stop.supposed_arrival_utc,
            stop.actual_arrival_utc,
            stop.arr_delay_s,
            stop.supposed_departure_utc,
            stop.actual_departure_utc,
            stop.dep_delay_s,
            stop.quality,
            stop.cancelled,
            stop.read_lag_s,
            stop.planned_platform,
            stop.changed_platform,
        )
    )
