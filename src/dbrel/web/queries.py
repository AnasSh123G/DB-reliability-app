"""Every database question the web asks, and no Django anywhere in it.

Keeping this module Django-free is load-bearing: it stays testable under plain
pytest, callable from the CLI, and it guarantees the web and the CLI cannot drift
into two different answers to the same question.
"""

from __future__ import annotations

import time
from datetime import date as date_cls
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dbrel.config import (
    AVG_MIN_SAMPLES,
    BOARD_BACK_S,
    BOARD_FWD_S,
    BOARD_LIMIT,
    SERVICE_DAY_MAX_S,
    TZ,
)
from dbrel.gtfs.timeutil import format_gtfs_time, local_date, service_date_anchor
from dbrel.identity import prefix_range, search_norm
from dbrel.models import (
    BASIS_REAL,
    BASIS_SYNTH,
    FeedVersion,
    SearchAlias,
    ServiceLine,
    Station,
    StationStop,
    Train,
    TrainStationDelay,
)

TT_FEED_KIND = "tt"

# Two characters is where a prefix scan stops returning most of the table. Below
# it, prefix_range('') returns ('', '') which matches nothing anyway — the guard
# is there to skip the round-trip, not to change the answer.
MIN_QUERY_LEN = 2


def tt_feed_version_id(session: Session) -> int | None:
    """Read-only counterpart to runs.tt_feed_version, which INSERTS when missing.

    A GET must never write, and rendering a board would otherwise create a
    feed_version row on a database that has never been imported.
    """
    return session.scalar(select(FeedVersion.id).where(FeedVersion.kind == TT_FEED_KIND))


def resolve_basis(session: Session, requested: str | None = None) -> str:
    """Whether the site is showing measured or simulated figures.

    Resolved once per request, for the whole page. The alternative — a badge on
    each row — is something a reader can miss; a page-level answer cannot be.
    """
    if requested:
        upper = requested.upper()
        if upper in (BASIS_REAL, BASIS_SYNTH):
            return upper
    # "Enough to say something", not merely "a row exists". After one day of
    # collection every real pair has exactly one sample, which is below the floor
    # the board will print — switching to REAL then would replace the simulated
    # figures with a page of em-dashes. This flips over on its own once the
    # collector has run enough days to carry a real average.
    usable_real = session.scalar(
        select(TrainStationDelay.id)
        .where(
            TrainStationDelay.basis == BASIS_REAL,
            TrainStationDelay.n_dep >= AVG_MIN_SAMPLES,
        )
        .limit(1)
    )
    return BASIS_REAL if usable_real else BASIS_SYNTH


def _hhmm(sec: int | None) -> str | None:
    """Clock time from a seconds-of-service-day offset.

    format_gtfs_time keeps hours past 24 ('25:10'), which is exactly right here:
    a stop after midnight belongs to the previous service day and saying '01:10'
    would hide which day's run it is.
    """
    if sec is None:
        return None
    return format_gtfs_time(sec)[:5]


# How a mixed result list is ordered once exact matches are out of the way. A
# line is more useful than any one of its three hundred numbered runs, and an
# unmonitored station is a dead end, so it sorts last.
_KIND_RANK = {"station": 0, "line": 1, "train": 2, "station_untracked": 3}


def suggest(session: Session, term: str, *, limit: int = 8) -> list[dict]:
    """Stations, lines and trains whose name starts with `term`.

    One prefix scan serves all three: search_alias already holds a row per
    searchable form of every entity, and ix_alias_lookup turns the half-open
    range into a seek.
    """
    normalised = search_norm(term or "")
    if len(normalised) < MIN_QUERY_LEN:
        return []
    lo, hi = prefix_range(normalised)

    rows = session.execute(
        select(
            SearchAlias.alias_norm,
            SearchAlias.alias_display,
            SearchAlias.weight,
            SearchAlias.station_id,
            SearchAlias.line_id,
            SearchAlias.train_id,
        )
        .where(SearchAlias.alias_norm >= lo, SearchAlias.alias_norm < hi)
        .order_by(
            (SearchAlias.alias_norm == normalised).desc(),
            SearchAlias.weight.desc(),
            SearchAlias.alias_norm,
        )
        .limit(limit * 20)
    ).all()
    if not rows:
        return []

    # Collect distinct targets in match order, then resolve them in three batch
    # queries rather than one per row.
    order: list[tuple[str, int]] = []
    exact: set[tuple[str, int]] = set()
    seen: set[tuple[str, int]] = set()
    for alias_norm, _display, _weight, station_id, line_id, train_id in rows:
        if station_id:
            key = ("station", station_id)
        elif line_id:
            key = ("line", line_id)
        elif train_id:
            key = ("train", train_id)
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        order.append(key)
        if alias_norm == normalised:
            exact.add(key)

    wanted: dict[str, list[int]] = {"station": [], "line": [], "train": []}
    for kind, ident in order:
        wanted[kind].append(ident)

    stations = {
        row.id: row
        for row in session.execute(
            select(Station).where(Station.id.in_(wanted["station"]))
        ).scalars()
    } if wanted["station"] else {}
    lines = {
        row.id: row
        for row in session.execute(
            select(ServiceLine).where(ServiceLine.id.in_(wanted["line"]))
        ).scalars()
    } if wanted["line"] else {}
    trains = {
        row.id: row
        for row in session.execute(
            select(Train).where(Train.id.in_(wanted["train"]))
        ).scalars()
    } if wanted["train"] else {}

    # How many numbered runs each matched line has, so the result can say so.
    run_counts: dict[int, int] = {}
    if wanted["line"]:
        run_counts = dict(
            session.execute(
                select(Train.line_id, func.count(Train.id))
                .where(Train.line_id.in_(wanted["line"]))
                .group_by(Train.line_id)
            ).all()
        )

    results: list[dict] = []
    for kind, ident in order:
        if kind == "station":
            station = stations.get(ident)
            if station is None:
                continue
            tracked = bool(station.tracked)
            results.append(
                {
                    "kind": "station",
                    "rank": _KIND_RANK["station" if tracked else "station_untracked"],
                    "exact": (kind, ident) in exact,
                    "id": station.id,
                    "name": station.name,
                    "eva": station.eva,
                    "tracked": tracked,
                    "sub": (f"Station · EVA {station.eva}" if station.eva else "Station")
                    + (" · monitored" if tracked else " · not monitored"),
                    "url": f"/station/{station.id}/",
                }
            )
        elif kind == "line":
            line = lines.get(ident)
            if line is None:
                continue
            n = run_counts.get(ident, 0)
            # IRIS publishes rail-replacement buses under the line they replace,
            # so the same label can name both. Saying which is the difference
            # between a useful result and a baffling duplicate.
            is_bus = (line.category or "").upper() == "BUS"
            results.append(
                {
                    "kind": "line",
                    "rank": _KIND_RANK["line"],
                    "exact": (kind, ident) in exact,
                    "id": line.id,
                    "name": line.display_name or line.identity_key,
                    "sub": (
                        f"Replacement bus · {n} run{'s' if n != 1 else ''}"
                        if is_bus
                        else f"Line · {line.operator or '?'} · {n} run{'s' if n != 1 else ''}"
                    ),
                    "replacement_bus": is_bus,
                    "url": f"/line/{line.id}/",
                }
            )
        else:
            train = trains.get(ident)
            if train is None:
                continue
            journey = " → ".join(x for x in (train.origin_name, train.dest_name) if x)
            results.append(
                {
                    "kind": "train",
                    "rank": _KIND_RANK["train"],
                    "exact": (kind, ident) in exact,
                    "id": train.id,
                    "name": train.display_name or f"#{train.id}",
                    "sub": f"Train · {journey}" if journey else "Train",
                    "url": f"/train/{train.id}/",
                }
            )

    results.sort(key=lambda r: (not r["exact"], r["rank"]))
    for r in results:
        r.pop("exact", None)
        r.pop("rank", None)
    return results[:limit]


def tracked_stations(session: Session) -> list[dict]:
    """The stations that actually have a timetable pattern, for the landing page.

    Only tracked stations are polled, so only they have a board. Out of ~7,900
    known stations that is a small handful, and leaving a visitor to guess which
    ones is the difference between a usable page and a dead end.
    """
    rows = session.execute(
        select(Station.id, Station.name, Station.eva)
        .where(Station.tracked == 1)
        .order_by(Station.name)
    ).all()
    return [
        {"id": station_id, "name": name, "eva": eva, "url": f"/station/{station_id}/"}
        for station_id, name, eva in rows
    ]


def board_slices(now_epoch: int, back_s: int, fwd_s: int) -> list[tuple[date_cls, int, int]]:
    """(service_date, lo_sec, hi_sec) slices covering [now-back, now+fwd].

    Planned times are seconds from a service-date anchor, not epochs, so a window
    in real time has to be projected onto each service day it can touch.

    Two things make this more than arithmetic. service_date_anchor uses noon-minus-
    twelve-hours rather than midnight, because on 2026-03-29 the local day is 23
    hours long and on 2026-10-25 it is 25 — naive midnight would shift every time
    on the board by an hour, twice a year, silently. And the previous service date
    has to be included: a run departing 23:50 on day D-1 carries
    planned_departure_sec ≈ 85800 and is still current at 00:10 on day D, so
    without that slice the board goes blank every night after midnight.
    """
    lo_epoch, hi_epoch = now_epoch - back_s, now_epoch + fwd_s
    candidates = dict.fromkeys(
        (
            local_date(lo_epoch) - timedelta(days=1),
            local_date(lo_epoch),
            local_date(hi_epoch),
        )
    )
    out: list[tuple[date_cls, int, int]] = []
    for service_date in candidates:
        anchor = service_date_anchor(service_date)
        lo, hi = lo_epoch - anchor, hi_epoch - anchor
        if hi < 0 or lo > SERVICE_DAY_MAX_S:
            continue
        out.append((service_date, max(lo, 0), min(hi, SERVICE_DAY_MAX_S)))
    return out


def _average(row, prefix: str) -> dict | None:
    """The delay summary for one call, or None when there is too little to say."""
    if row is None:
        return None
    count = row.n_dep if prefix == "dep" else row.n_arr
    if not count or count < AVG_MIN_SAMPLES:
        return None
    avg = getattr(row, f"avg_{prefix}_delay_s")
    if avg is None:
        return None
    punctual = getattr(row, f"pct_{prefix}_punctual")
    cancel_rate = (row.n_cancelled / row.n_known) if row.n_known else None
    return {
        "avg_s": round(avg),
        "avg_min": round(avg / 60, 1),
        "p50_min": round((getattr(row, f"p50_{prefix}_delay_s") or 0) / 60, 1),
        "p90_min": round((getattr(row, f"p90_{prefix}_delay_s") or 0) / 60, 1),
        "punctual": punctual,
        # Templates have no percent filter, and a rate the reader has to multiply
        # in their head is a rate they will misread.
        "punctual_pct": round(punctual * 100) if punctual is not None else None,
        "n": count,
        "n_runs": row.n_runs,
        "cancel_rate": cancel_rate,
        "cancel_pct": round(cancel_rate * 100, 1) if cancel_rate else None,
    }


def _clock(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, TZ).strftime("%H:%M")


def station_board(
    session: Session,
    station_id: int,
    *,
    basis: str | None = None,
    now: int | None = None,
    back_s: int = BOARD_BACK_S,
    fwd_s: int = BOARD_FWD_S,
    limit: int = BOARD_LIMIT,
) -> dict | None:
    """Everything calling at one station in a window, with its usual lateness."""
    station = session.get(Station, station_id)
    if station is None:
        return None

    now = now or int(time.time())
    basis = resolve_basis(session, basis)
    fv_id = tt_feed_version_id(session)

    payload = {
        "station": {
            "id": station.id,
            "name": station.name,
            "eva": station.eva,
            "tracked": bool(station.tracked),
        },
        "basis": basis,
        "window": {
            "from": _clock(now - back_s),
            "to": _clock(now + fwd_s),
            "date": local_date(now).isoformat(),
        },
        "calls": [],
    }
    if fv_id is None:
        return payload

    # Keyed by (station_stop.id, service_date): a through call has both an arrival
    # and a departure and must appear once carrying both times, but a station_stop
    # is a *pattern* rather than a dated run, so the same row can legitimately land
    # on two service dates once the window is widened past a day.
    calls: dict[tuple[int, str], dict] = {}

    for service_date, lo, hi in board_slices(now, back_s, fwd_s):
        anchor = service_date_anchor(service_date)
        for column in (StationStop.planned_departure_sec, StationStop.planned_arrival_sec):
            rows = session.execute(
                select(StationStop, Train, TrainStationDelay)
                .join(Train, Train.id == StationStop.train_id)
                .outerjoin(
                    TrainStationDelay,
                    (TrainStationDelay.station_id == StationStop.station_id)
                    & (TrainStationDelay.train_id == StationStop.train_id)
                    & (TrainStationDelay.basis == basis),
                )
                .where(
                    StationStop.station_id == station_id,
                    # Not optional: without it the board mixes in the GTFS skeleton
                    # trains, which carry no train numbers and use a different
                    # stop_sequence convention.
                    StationStop.feed_version_id == fv_id,
                    column >= lo,
                    column < hi,
                )
                .order_by(column)
                .limit(limit)
            ).all()

            for stop, train, delay in rows:
                key = (stop.id, service_date.isoformat())
                entry = calls.get(key)
                if entry is None:
                    arr_epoch = (
                        anchor + stop.planned_arrival_sec
                        if stop.planned_arrival_sec is not None
                        else None
                    )
                    dep_epoch = (
                        anchor + stop.planned_departure_sec
                        if stop.planned_departure_sec is not None
                        else None
                    )
                    # A train that arrives and does not leave again terminates
                    # here, and naming its destination would just repeat the
                    # station you are already looking at.
                    terminates = dep_epoch is None and arr_epoch is not None
                    entry = {
                        "station_stop_id": stop.id,
                        "train_id": train.id,
                        "terminates": terminates,
                        "headline": (
                            (train.origin_name and f"from {train.origin_name}")
                            if terminates
                            else (train.dest_name and f"to {train.dest_name}")
                        )
                        or "",
                        "train": train.display_name or train.search_name or f"#{train.id}",
                        "category": train.category,
                        "segment": train.segment,
                        "number": train.train_number,
                        "platform": stop.platform_code,
                        "origin": train.origin_name,
                        "destination": train.dest_name,
                        "service_date": service_date.isoformat(),
                        "arr": _clock(arr_epoch),
                        "dep": _clock(dep_epoch),
                        "sort_key": dep_epoch or arr_epoch or 0,
                        "arr_avg": _average(delay, "arr"),
                        "dep_avg": _average(delay, "dep"),
                    }
                    calls[key] = entry

    payload["calls"] = sorted(calls.values(), key=lambda c: c["sort_key"])[:limit]
    return payload


def train_route(session: Session, train_id: int, *, basis: str | None = None) -> dict | None:
    """One train's whole journey, with scheduled times and typical lateness.

    The full route comes from train.route_path — IRIS's own ppth, every stop the
    service makes. Times and averages come from station_stop and
    train_station_delay, which exist only for the stations we actually poll, so
    most rows carry a name and nothing else. Showing the whole journey and
    marking which parts are measured is more honest than showing only the three
    stops we happen to watch and letting it read like the entire route.
    """
    train = session.get(Train, train_id)
    if train is None:
        return None

    basis = resolve_basis(session, basis)
    fv_id = tt_feed_version_id(session)

    measured: dict[int, tuple] = {}
    if fv_id is not None:
        for stop, station, delay in session.execute(
            select(StationStop, Station, TrainStationDelay)
            .join(Station, Station.id == StationStop.station_id)
            .outerjoin(
                TrainStationDelay,
                (TrainStationDelay.station_id == StationStop.station_id)
                & (TrainStationDelay.train_id == StationStop.train_id)
                & (TrainStationDelay.basis == basis),
            )
            .where(
                StationStop.train_id == train_id,
                StationStop.feed_version_id == fv_id,
            )
        ).all():
            measured[stop.stop_sequence] = (stop, station, delay)

    # stop_sequence is the 1-based index into ppth, so the two line up directly.
    names = [part for part in (train.route_path or "").split("|") if part]
    if not names and measured:
        names = [""] * max(measured)

    stops: list[dict] = []
    for index, name in enumerate(names, start=1):
        entry = {
            "seq": index,
            "name": name,
            "monitored": False,
            "station_id": None,
            "arr": None,
            "dep": None,
            "platform": None,
            "arr_avg": None,
            "dep_avg": None,
        }
        hit = measured.get(index)
        if hit is not None:
            stop, station, delay = hit
            entry.update(
                {
                    "monitored": True,
                    "station_id": station.id,
                    # The station page is keyed on our own station row, so link by
                    # id and sidestep the IRIS/gtfs.de name disagreement entirely.
                    "url": f"/station/{station.id}/",
                    "arr": _hhmm(stop.planned_arrival_sec),
                    "dep": _hhmm(stop.planned_departure_sec),
                    "platform": stop.platform_code,
                    "arr_avg": _average(delay, "arr"),
                    "dep_avg": _average(delay, "dep"),
                }
            )
        stops.append(entry)

    graded = [s["dep_avg"] or s["arr_avg"] for s in stops]
    graded = [g for g in graded if g]
    line = session.get(ServiceLine, train.line_id) if train.line_id else None

    return {
        "train": {
            "id": train.id,
            "name": train.display_name or f"#{train.id}",
            "category": train.category,
            "segment": train.segment,
            "number": train.train_number,
            "operator": train.operator,
            "origin": train.origin_name,
            "destination": train.dest_name,
            "n_stops": train.n_stops or len(names),
            "line": {"id": line.id, "name": line.display_name} if line else None,
            "replacement_bus": (train.category or "").upper() == "BUS",
        },
        "basis": basis,
        "n_monitored": sum(1 for s in stops if s["monitored"]),
        # A journey-level headline, averaged over the stops we can actually see.
        "avg_delay_min": (
            round(sum(g["avg_min"] for g in graded) / len(graded), 1) if graded else None
        ),
        "stops": stops,
    }


def line_detail(session: Session, line_id: int, *, limit: int = 300) -> dict | None:
    """A line and every numbered run on it, earliest departure first."""
    line = session.get(ServiceLine, line_id)
    if line is None:
        return None

    fv_id = tt_feed_version_id(session)
    monitored = (
        dict(
            session.execute(
                select(StationStop.train_id, func.count(StationStop.id))
                .join(Train, Train.id == StationStop.train_id)
                .where(Train.line_id == line_id, StationStop.feed_version_id == fv_id)
                .group_by(StationStop.train_id)
            ).all()
        )
        if fv_id is not None
        else {}
    )

    trains = session.execute(
        select(Train)
        .where(Train.line_id == line_id)
        .order_by(Train.first_departure_sec, Train.train_number)
        .limit(limit)
    ).scalars().all()

    return {
        "line": {
            "id": line.id,
            "name": line.display_name or line.identity_key,
            "category": line.category,
            "operator": line.operator,
            "replacement_bus": (line.category or "").upper() == "BUS",
        },
        "n_runs": len(trains),
        "runs": [
            {
                "id": t.id,
                "name": t.display_name or f"#{t.id}",
                "number": t.train_number,
                "origin": t.origin_name,
                "destination": t.dest_name,
                "departs": _hhmm(t.first_departure_sec),
                "n_stops": t.n_stops,
                "n_monitored": monitored.get(t.id, 0),
                "url": f"/train/{t.id}/",
            }
            for t in trains
        ],
    }
