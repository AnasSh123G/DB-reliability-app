"""Command line interface. Every command is one-shot: it runs, reports, exits."""

from __future__ import annotations

from datetime import datetime, timedelta

import typer
from sqlalchemy import func, select

from dbrel.config import DB_PATH, STATIC_FEEDS
from dbrel.db import create_all, session_scope
from dbrel.gtfs.download import fetch_static
from dbrel.gtfs.static_import import import_feed
from dbrel.models import (
    TRUSTED_QUALITIES,
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
    create_all()
    with session_scope() as session:
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
        ]:
            count = session.scalar(select(func.count()).select_from(model))
            typer.echo(f"  {label:20s} {count:>12,}")

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
    """Show realtime trips that resolved to no known train."""
    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(RtUnmatched))
        typer.echo(f"{total:,} unmatched realtime trips")
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
