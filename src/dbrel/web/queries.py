"""Every database question the web asks, and no Django anywhere in it.

Keeping this module Django-free is load-bearing: it stays testable under plain
pytest, callable from the CLI, and it guarantees the web and the CLI cannot drift
into two different answers to the same question.
"""

from __future__ import annotations

import bisect
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
from dbrel.holidays_de import day_type_of, load_holiday_dates
from dbrel.identity import prefix_range, search_norm
from dbrel.models import (
    DAY_TYPE_WEEKDAY,
    FeedVersion,
    SearchAlias,
    ServiceLine,
    Station,
    StationStop,
    Train,
    TrainStationDelay,
    TrainStopAverage,
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


_SUGGEST_KIND_COLUMN = {
    "station": SearchAlias.station_id,
    "line": SearchAlias.line_id,
    "train": SearchAlias.train_id,
}


def suggest(
    session: Session, term: str, *, limit: int = 8, only: str | None = None
) -> list[dict]:
    """Stations, lines and trains whose name starts with `term`.

    One prefix scan serves all three: search_alias already holds a row per
    searchable form of every entity, and ix_alias_lookup turns the half-open
    range into a seek. `only` narrows the scan to one kind, for a picker that
    only ever wants lines, say, rather than filtering the general result client
    side and hoping a line survives the top `limit` matches.
    """
    normalised = search_norm(term or "")
    if len(normalised) < MIN_QUERY_LEN:
        return []
    lo, hi = prefix_range(normalised)

    conditions = [SearchAlias.alias_norm >= lo, SearchAlias.alias_norm < hi]
    if only is not None:
        conditions.append(_SUGGEST_KIND_COLUMN[only].is_not(None))

    rows = session.execute(
        select(
            SearchAlias.alias_norm,
            SearchAlias.alias_display,
            SearchAlias.weight,
            SearchAlias.station_id,
            SearchAlias.line_id,
            SearchAlias.train_id,
        )
        .where(*conditions)
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
    """The delay summary for one call, or None when there is nothing to say at all.

    Shown from the first sample rather than hidden below AVG_MIN_SAMPLES: hiding
    it would mean every board reads "not enough observations" for roughly the
    first five service days. The count is always printed alongside it, and
    "provisional" marks it below the threshold, so a reader is never misled into
    treating one run as settled.
    """
    if row is None:
        return None
    count = row.n_dep if prefix == "dep" else row.n_arr
    if not count:
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
        "provisional": count < AVG_MIN_SAMPLES,
    }


def _clock(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, TZ).strftime("%H:%M")


def station_board(
    session: Session,
    station_id: int,
    *,
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
    fv_id = tt_feed_version_id(session)

    payload = {
        "station": {
            "id": station.id,
            "name": station.name,
            "eva": station.eva,
            "tracked": bool(station.tracked),
        },
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
                    # Averages are per day of week, so the board shows the figure for
                    # the day being looked at -- a Sunday board quoting a Tuesday's
                    # delays would be wrong, and joining without this would return
                    # seven rows per call and duplicate every entry on the board.
                    & (TrainStationDelay.dow == service_date.weekday()),
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


def train_route(
    session: Session, train_id: int, *, day_type: str = DAY_TYPE_WEEKDAY
) -> dict | None:
    """One train's whole journey, with scheduled times and typical lateness.

    The full route comes from train.route_path — IRIS's own ppth, every stop the
    service makes. Times and averages come from station_stop and
    train_stop_average, which exist only for the stations we actually poll, so
    most rows carry a name and nothing else. Showing the whole journey and
    marking which parts are measured is more honest than showing only the three
    stops we happen to watch and letting it read like the entire route.

    Averages are the Monday-to-Saturday pool, not one weekday: a run happens once
    a day, so splitting by weekday leaves a single sample per row and the page
    reads "no observations yet" for every stop on any day we have not collected.
    `day_type` switches to the Sunday and public-holiday pool instead.
    """
    train = session.get(Train, train_id)
    if train is None:
        return None

    fv_id = tt_feed_version_id(session)

    measured: dict[int, tuple] = {}
    if fv_id is not None:
        for stop, station, delay in session.execute(
            select(StationStop, Station, TrainStopAverage)
            .join(Station, Station.id == StationStop.station_id)
            .outerjoin(
                TrainStopAverage,
                (TrainStopAverage.station_id == StationStop.station_id)
                & (TrainStopAverage.train_id == StationStop.train_id)
                & (TrainStopAverage.day_type == day_type),
            )
            .where(
                StationStop.train_id == train_id,
                StationStop.feed_version_id == fv_id,
            )
            # A handful of (train, station) pairs carry two scheduled times, one
            # per timetable variant. Ordering puts the better-observed one last,
            # so it is the one that survives into `measured`.
            .order_by(TrainStopAverage.n_dep)
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
        "n_monitored": sum(1 for s in stops if s["monitored"]),
        # A journey-level headline, averaged over the stops we can actually see.
        # Provisional whenever any contributing stop is -- a headline resting on
        # one-sample stops needs the same caveat those stops carry individually.
        "avg_delay_min": (
            round(sum(g["avg_min"] for g in graded) / len(graded), 1) if graded else None
        ),
        "avg_delay_n_stops": len(graded),
        "avg_delay_provisional": any(g["provisional"] for g in graded) if graded else False,
        "stops": stops,
    }


def _canonical_order(trains) -> list[str]:
    """The longest route on the line, taken as the axis every run travels along."""
    best: list[str] = []
    for train in trains:
        names = [part for part in (train.route_path or "").split("|") if part]
        if len(names) > len(best):
            best = names
    return best


def _direction_of(train, index: dict[str, int]) -> int:
    """0 if the run travels the canonical way along the line, 1 if it runs back.

    Endpoints alone cannot answer this. RB33's short workings terminate at
    Mönchengladbach and Herzogenrath rather than at either end of the line, so its
    six origin/destination pairs describe only two directions. Position along the
    longest route does answer it, and the short workings sort into the right half
    without anyone having to enumerate them.
    """
    seen = [index[name] for name in (train.route_path or "").split("|") if name in index]
    if len(seen) < 2:
        ends = (index.get(train.origin_name), index.get(train.dest_name))
        seen = [position for position in ends if position is not None]
    if len(seen) < 2 or seen[-1] == seen[0]:
        return 0
    return 0 if seen[-1] > seen[0] else 1


def _run_averages(session: Session, line_id: int, day_type: str) -> dict[int, dict]:
    """One lateness figure per run, pooled over every stop of it we measure."""
    rows = session.execute(
        select(
            TrainStopAverage.train_id,
            func.sum(TrainStopAverage.n_dep),
            func.sum(TrainStopAverage.avg_dep_delay_s * TrainStopAverage.n_dep),
            func.sum(TrainStopAverage.pct_dep_punctual * TrainStopAverage.n_dep),
            func.max(TrainStopAverage.n_runs),
        )
        .join(Train, Train.id == TrainStopAverage.train_id)
        .where(
            Train.line_id == line_id,
            TrainStopAverage.day_type == day_type,
            TrainStopAverage.n_dep > 0,
            TrainStopAverage.avg_dep_delay_s.is_not(None),
        )
        .group_by(TrainStopAverage.train_id)
    ).all()

    averages: dict[int, dict] = {}
    for train_id, n_dep, delay_sum, punctual_sum, n_runs in rows:
        if not n_dep:
            continue
        averages[train_id] = {
            "avg_min": round(delay_sum / n_dep / 60, 1),
            "punctual_pct": (
                round(punctual_sum / n_dep * 100) if punctual_sum is not None else None
            ),
            # Counted in runs rather than stops: one day's run calling at ten
            # measured stops is one observation of that run, not ten.
            "n_runs": n_runs or 0,
            "provisional": (n_runs or 0) < AVG_MIN_SAMPLES,
        }
    return averages


def _line_trains_by_direction(
    session: Session, line_id: int, *, limit: int = 300
) -> tuple[list[str], list[list[Train]]]:
    """Every numbered run on a line, split into its two directions of travel.

    Shared by every view that needs "the runs going this way": the line page, the
    direction/station pickers, and the around-this-time lookup all have to agree
    on which train goes which way, so the split lives here once.
    """
    trains = session.execute(
        select(Train)
        .where(Train.line_id == line_id)
        .order_by(Train.first_departure_sec, Train.train_number)
        .limit(limit)
    ).scalars().all()

    canonical = _canonical_order(trains)
    index = {name: position for position, name in enumerate(canonical)}
    halves: list[list[Train]] = [[], []]
    for t in trains:
        halves[_direction_of(t, index)].append(t)
    return canonical, halves


def _direction_label(canonical: list[str], direction: int) -> str:
    if not canonical:
        return "all runs"
    return f"towards {canonical[-1 if direction == 0 else 0]}"


def line_directions(session: Session, line_id: int) -> dict | None:
    """The line's two directions, for a picker to show once a line is chosen."""
    line = session.get(ServiceLine, line_id)
    if line is None:
        return None

    canonical, halves = _line_trains_by_direction(session, line_id)
    return {
        "line": {"id": line.id, "name": line.display_name or line.identity_key},
        "directions": [
            {"direction": direction, "label": _direction_label(canonical, direction),
             "n_runs": len(trains)}
            for direction, trains in enumerate(halves)
            if trains
        ],
    }


def line_stations(session: Session, line_id: int, direction: int) -> list[dict] | None:
    """Monitored stations a direction of a line calls at, in travel order.

    Only stations with a station_stop row can go on here: that is exactly the set
    with a scheduled time to compare against, which is the entire point of the
    next step.

    Ordering by matching Station.name against the text in train.route_path was
    the first attempt, and it silently scrambled the list: IRIS's ppth and our
    own station names disagree on plenty of stops (route_path says
    "Krefeld-Uerdingen", the station row says "Krefeld Uerdingen Bf"), which is
    exactly the mismatch train_route already routes around by joining on
    station_id instead. This does the same -- order by stop_sequence on whichever
    train in this direction covers the most stations, id-to-id throughout.
    """
    line = session.get(ServiceLine, line_id)
    if line is None or direction not in (0, 1):
        return None

    _, halves = _line_trains_by_direction(session, line_id)
    train_ids = [t.id for t in halves[direction]]
    fv_id = tt_feed_version_id(session)
    if not train_ids or fv_id is None:
        return []

    rows = session.execute(
        select(StationStop.train_id, StationStop.station_id, StationStop.stop_sequence, Station.name)
        .join(Station, Station.id == StationStop.station_id)
        .where(StationStop.train_id.in_(train_ids), StationStop.feed_version_id == fv_id)
    ).all()
    if not rows:
        return []

    coverage: dict[int, int] = {}
    for train_id, _station_id, _seq, _name in rows:
        coverage[train_id] = coverage.get(train_id, 0) + 1
    reference_id = max(coverage, key=coverage.get)

    position = {
        station_id: seq
        for train_id, station_id, seq, _name in rows
        if train_id == reference_id
    }
    names: dict[int, str] = {}
    for _train_id, station_id, _seq, name in rows:
        names.setdefault(station_id, name)

    # A station only a non-reference train serves (a short working's own extra
    # stop, say) has no position from the reference train -- keep it, just after
    # everything the reference train does cover, rather than dropping it.
    fallback = max(position.values(), default=0) + 1
    ordered_ids = sorted(names, key=lambda sid: position.get(sid, fallback))
    return [{"id": station_id, "name": names[station_id]} for station_id in ordered_ids]


def resolve_day_type(session: Session, service_date: str) -> str | None:
    """Which average pool a calendar date reads from -- WEEKDAY or SUNDAY_HOLIDAY.

    Checks the public_holiday table (day_type_of), not just the day of the week,
    so a date entered on the browse widget gets whichever pool the holiday table
    says it belongs to -- including a date that is a holiday for no reason a
    calendar rule would know about.
    """
    return day_type_of(service_date, load_holiday_dates(session))


def trips_around(
    session: Session,
    line_id: int,
    direction: int,
    station_id: int,
    time_of_day_s: int,
    *,
    day_type: str = DAY_TYPE_WEEKDAY,
) -> dict | None:
    """The 2 scheduled calls at or before a time, and the 1 right after.

    The schedule is a recurring daily pattern -- that is the point of asking for
    a time of day rather than a timestamp -- so "before" and "after" wrap around
    midnight rather than running out at either end of the day. `day_type` picks
    which pooled average is attached to each trip; it does not affect which
    trips are considered, since the underlying timetable pattern is itself
    dateless. Callers with an actual calendar date use resolve_day_type first.
    """
    line = session.get(ServiceLine, line_id)
    station = session.get(Station, station_id)
    if line is None or station is None or direction not in (0, 1):
        return None

    _, halves = _line_trains_by_direction(session, line_id)
    trains_by_id = {t.id: t for t in halves[direction]}
    fv_id = tt_feed_version_id(session)

    payload = {
        "line": {"id": line.id, "name": line.display_name or line.identity_key},
        "direction": direction,
        "station": {"id": station.id, "name": station.name},
        "trips": [],
    }
    if not trains_by_id or fv_id is None:
        return payload

    rows = session.execute(
        select(StationStop, TrainStopAverage)
        .outerjoin(
            TrainStopAverage,
            (TrainStopAverage.station_id == StationStop.station_id)
            & (TrainStopAverage.train_id == StationStop.train_id)
            & (TrainStopAverage.day_type == day_type),
        )
        .where(
            StationStop.station_id == station_id,
            StationStop.train_id.in_(list(trains_by_id)),
            StationStop.feed_version_id == fv_id,
        )
    ).all()

    candidates = []
    for stop, avg in rows:
        sched = stop.planned_departure_sec
        if sched is None:
            sched = stop.planned_arrival_sec
        candidates.append((sched % 86400, stop, avg))
    candidates.sort(key=lambda c: c[0])

    n = len(candidates)
    if n == 0:
        return payload

    times = [c[0] for c in candidates]
    after_idx = bisect.bisect_right(times, time_of_day_s)

    # The two before/at and the one after, wrapping through index 0 when the
    # chosen time is near either edge of the day. With fewer than 3 runs total
    # the same stop is picked more than once; dedup on the way out rather than
    # pretending three distinct trips exist when they do not.
    # Position in this 3-slot picture, not a fresh tod comparison: wrapping past
    # midnight can put the "next" call earlier in the day than the chosen time
    # (23:00 -> next is 06:00), and it must still read as "after", not "before".
    picks = [
        (i % n, "before" if slot < 2 else "after")
        for slot, i in enumerate(((after_idx - 2), (after_idx - 1), after_idx))
    ]

    seen: set[int] = set()
    trips = []
    for i, relation in picks:
        _tod, stop, avg = candidates[i]
        if stop.id in seen:
            continue
        seen.add(stop.id)
        train = trains_by_id[stop.train_id]
        trips.append(
            {
                "train_id": train.id,
                "name": train.display_name or f"#{train.id}",
                "url": f"/train/{train.id}/",
                "sched_arr": _hhmm(stop.planned_arrival_sec),
                "sched_dep": _hhmm(stop.planned_departure_sec),
                "arr_avg": _average(avg, "arr"),
                "dep_avg": _average(avg, "dep"),
                "relation": relation,
            }
        )
    payload["trips"] = trips
    return payload


def line_detail(
    session: Session,
    line_id: int,
    *,
    limit: int = 300,
    current_train_id: int | None = None,
    day_type: str = DAY_TYPE_WEEKDAY,
) -> dict | None:
    """A line and every numbered run on it, split by direction of travel.

    Both directions of a line share a name but never a train: each direction gets
    its own train numbers, so it already has its own train_id and its own averages.
    Listing them interleaved by departure time -- which is what a line page did
    before -- hides that, and reads as one service running twice as often.
    """
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

    canonical, halves = _line_trains_by_direction(session, line_id, limit=limit)
    averages = _run_averages(session, line_id, day_type)

    result_halves: list[list[dict]] = [[], []]
    for direction, trains in enumerate(halves):
        for t in trains:
            result_halves[direction].append(
                {
                    "id": t.id,
                    "name": t.display_name or f"#{t.id}",
                    "number": t.train_number,
                    "segment": t.segment,
                    "origin": t.origin_name,
                    "destination": t.dest_name,
                    "departs": _hhmm(t.first_departure_sec),
                    "n_stops": t.n_stops,
                    "n_monitored": monitored.get(t.id, 0),
                    "url": f"/train/{t.id}/",
                    # A short working: runs part of the line rather than all of it.
                    "partial": bool(canonical) and (t.n_stops or 0) < len(canonical),
                    "current": t.id == current_train_id,
                    "avg": averages.get(t.id),
                }
            )

    return {
        "line": {
            "id": line.id,
            "name": line.display_name or line.identity_key,
            "category": line.category,
            "operator": line.operator,
            "replacement_bus": (line.category or "").upper() == "BUS",
        },
        "n_runs": sum(len(runs) for runs in result_halves),
        "directions": [
            {
                "label": _direction_label(canonical, direction),
                "n_runs": len(runs),
                "runs": runs,
            }
            for direction, runs in enumerate(result_halves)
        ],
    }
