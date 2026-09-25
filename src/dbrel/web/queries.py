"""Every database question the web asks, and no Django anywhere in it.

Keeping this module Django-free is load-bearing: it stays testable under plain
pytest, callable from the CLI, and it guarantees the web and the CLI cannot drift
into two different answers to the same question.
"""

from __future__ import annotations

import bisect
import json
import time
from datetime import date as date_cls
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dbrel.config import (
    BOARD_BACK_S,
    BOARD_FWD_S,
    BOARD_LIMIT,
    ML_MODELS_DIR,
    SERVICE_DAY_MAX_S,
    TZ,
)
from dbrel.ingest.timeutil import format_gtfs_time, local_date, service_date_anchor
from dbrel.direction import (
    canonical_order,
    corridor_trains_by_direction,
    direction_by_train_id,
    direction_of,
    line_trains_by_direction,
)
from dbrel.holidays_de import load_holiday_dates
from dbrel.identity import prefix_range, search_norm
from dbrel.ml.bakeoff import MANIFEST_NAME, current_artifact
from dbrel.ml.features import UNKNOWN_DIRECTION, compute_features, direction_for, encode_categoricals
from dbrel.ml.nn import model_inputs
from dbrel.ml.timetable import calls_at
from dbrel.models import (
    DAY_TYPE_SUNDAY_HOLIDAY,
    DAY_TYPE_WEEKDAY,
    FeedVersion,
    SearchAlias,
    ServiceLine,
    Station,
    StationStop,
    StopDelayAverage,
    Train,
    TrainStopDelayAverage,
    Trip,
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


def _is_holiday_flag(day_type: str) -> int:
    return 1 if day_type == DAY_TYPE_SUNDAY_HOLIDAY else 0


def _train_trip_start_time(train: Train) -> str | None:
    """Mod-24 station-local 'HH:MM' proxy for a train pattern's origin departure.

    This is what stop_delay_average keys GTFS-sourced trips on exactly
    (derive/stop_averages.py falls back to `first_departure_sec % 86400` when a
    trip has no tts run id), and a fair approximation for the tts-sourced
    majority: a train number only has more than one true daily start time in
    rare coupled/split cases (see dbrel-aggregates memory), which these
    pattern-level query functions can't tell apart since they work off `Train`,
    not individual dated `Trip` rows.
    """
    if train.first_departure_sec is None:
        return None
    sec = train.first_departure_sec % 86400
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"


def _sched_time_key(arr_sec: int | None, dep_sec: int | None) -> str | None:
    """Mod-24 station-local 'HH:MM' for a stop_delay_average lookup key.

    stop_delay_average keys BOTH sides of a stop under ONE row -- built from
    `COALESCE(ts.supposed_departure_utc, ts.supposed_arrival_utc)`
    (derive/stop_averages.py), i.e. the departure's local time if there is
    one, else the arrival's. So arr_pred and dep_pred for the same stop must
    look up the SAME sched_time (this function), never each side's own raw
    time -- only `side` in `_delay_pred` picks which column of that one row
    to read.

    Deliberately separate from `_hhmm`: display keeps service-day notation (a
    post-midnight stop shows '25:10', not '01:10', so it's clear which day's
    run it belongs to), but stop_delay_average's own `sched_time` was built
    from actual local wall-clock time, so the lookup key needs the wrapped
    hour, not the service-day one.
    """
    sec = dep_sec if dep_sec is not None else arr_sec
    if sec is None:
        return None
    sec = sec % 86400
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"


def _delay_pred(
    session: Session,
    *,
    station_id: int | None,
    line_id: int | None,
    direction: int | None,
    trip_start_time: str | None,
    sched_time: str | None,
    is_holiday: int,
    side: str,
    train_id: int | None = None,
) -> dict | None:
    """A stop's historical mean delay, or None.

    From stop_delay_average for a train with a line; from the per-train
    train_stop_delay_average for one without (ICE, IC, ...), which is why
    `train_id` is passed. This is the only "expected"/"predicted" delay figure
    dbrel has -- there is no live model. None (never a fabricated 0 or an
    invented interval) means either no matching row, or a row whose relevant
    side has zero samples -- see the web-frontend spec's "No average exists
    for the stop" requirement. `lo_min`/`hi_min` are set equal to `p50_min`:
    the tables hold a mean, not an interval, and the JSON shape is kept rather
    than resurveyed since nothing outside this app depends on it.
    """
    if station_id is None or trip_start_time is None or sched_time is None:
        return None
    if line_id is not None:
        if direction is None:
            return None
        row = session.get(
            StopDelayAverage,
            (station_id, line_id, direction, trip_start_time, sched_time, is_holiday),
        )
    elif train_id is not None:
        row = session.get(
            TrainStopDelayAverage,
            (train_id, station_id, trip_start_time, sched_time, is_holiday),
        )
    else:
        return None
    if row is None:
        return None
    n = row.n_dep if side == "dep" else row.n_arr
    mean_s = row.mean_dep_delay_s if side == "dep" else row.mean_arr_delay_s
    if not n or mean_s is None:
        return None
    p50 = round(mean_s / 60)
    return {"p50_min": p50, "lo_min": p50, "hi_min": p50}


# The delay model is served to the browser (add-browser-delay-predictions): the
# web layer encodes a call's features and hands the browser the tensors its
# ONNX runtime needs, but never itself loads onnxruntime or runs inference.
# Only the manifest (vocabulary, per-feature stats, cards) is loaded here --
# small (under a megabyte) and cheap to cache in the process, unlike the 20 MB
# model weights, which the browser fetches directly (see views.model_file).
_model_cache: dict = {"stamp": None, "manifest": None}


def current_model() -> tuple[str, dict] | None:
    """The current model artifact's stamp and manifest, or None if none is trained.

    Cached until the `CURRENT` pointer moves to a different artifact -- a bake-off
    or export run never touches an existing artifact in place (see bakeoff.py),
    so a cached manifest is never stale for the stamp it was loaded under.
    """
    artifact = current_artifact(ML_MODELS_DIR)
    if artifact is None:
        return None
    stamp = artifact.name
    if _model_cache["stamp"] != stamp:
        _model_cache["manifest"] = json.loads((artifact / MANIFEST_NAME).read_text())
        _model_cache["stamp"] = stamp
    return stamp, _model_cache["manifest"]


class _ModelDirectionCache:
    """A call site's own direction_for cache, reusing direction_by_train_id's
    existing per-line result instead of recomputing it once per train.

    direction_for(session, train) is correct but, for a train with a line,
    always recomputes direction_by_train_id(line_id) from scratch -- a
    per-line computation expensive enough (see direction.py's
    _connected_components) that computing it once per distinct train rather
    than once per distinct line noticeably slowed a busy station board (some
    lines carry many trains, e.g. S-Bahn lines).

    Pass the call site's own line_id-keyed direction cache, if it already
    keeps one for the averages lookups (station_board's `direction_cache`),
    so the two never independently pay for the same line -- the point of this
    class is not to compute this more cheaply than direction_for does, only
    to never compute the same line's result twice.
    """

    def __init__(self, line_direction_cache: dict[int, dict[int, int]] | None = None) -> None:
        self._by_line: dict[int, dict[int, int]] = (
            line_direction_cache if line_direction_cache is not None else {}
        )
        self._by_train: dict[int, int] = {}

    def get(self, session: Session, train: Train) -> int:
        if train.line_id is not None:
            dirs = self._by_line.get(train.line_id)
            if dirs is None:
                dirs = direction_by_train_id(session, train.line_id)
                self._by_line[train.line_id] = dirs
            return dirs.get(train.id, UNKNOWN_DIRECTION)
        if train.id not in self._by_train:
            self._by_train[train.id] = direction_for(session, train)
        return self._by_train[train.id]


def _model_input(
    session: Session,
    manifest: dict,
    *,
    train: Train,
    station_id: int | None,
    service_date: date_cls,
    direction: int,
    holiday_set: set[tuple[str, str]],
    arr_sec: int | None,
    dep_sec: int | None,
) -> dict | None:
    """This call's encoded model input, or None.

    Reuses dbrel.ml.features/dbrel.ml.timetable exactly as predict.py's offline
    path does (compute_features -> encode_categoricals -> model_inputs), so the
    arrays match what CLI prediction would compute for the same call bit for
    bit -- see the delay-model spec's "Encoded input matches what prediction
    would use" scenario. This stops one step short of running the model: no
    onnxruntime session is created here, only numpy arrays the browser's
    ONNX Runtime Web session will consume as its `cat`/`num` inputs.

    One input covers both arr and dep -- the model predicts both sides from one
    row (see nn.py's SIDES). Whether a side's prediction is actually shown is
    gated on the frontend by whether that side has a scheduled time at all,
    exactly like a missing average already is.
    """
    if station_id is None:
        return None
    calls = calls_at(session, train, station_id, service_date)
    if not calls:
        return None
    call = calls[0]
    if len(calls) > 1:
        # A looped route calls_at returns more than one visit for; match the
        # specific one these times belong to rather than always taking the
        # first (see ScheduledCall's own "a loop gives two" note).
        for candidate in calls:
            if (candidate.planned_arrival_sec, candidate.planned_departure_sec) == (
                arr_sec,
                dep_sec,
            ):
                call = candidate
                break
    raw = call.raw(direction=direction, holiday_set=holiday_set)
    features = encode_categoricals(compute_features(raw), manifest["vocab"])
    cats, nums = model_inputs(
        features, cards=manifest["cards"], numeric=manifest["numeric"], stats=manifest["stats"]
    )
    return {"cat": cats[0].tolist(), "num": nums[0].tolist()}


# How a mixed result list is ordered once exact matches are out of the way. A
# line is more useful than any one of its three hundred numbered runs, and an
# unmonitored station is a dead end, so it sorts last.
_KIND_RANK = {"station": 0, "line": 1, "train": 2, "station_untracked": 3}


_SUGGEST_KIND_COLUMN = {
    "station": SearchAlias.station_id,
    "line": SearchAlias.line_id,
    "train": SearchAlias.train_id,
}

# A service_line whose "line" is just a generic product code -- IRIS echoing
# the train category ("RE", "RB", "S", ...) where a real line name belongs. It
# names no actual route, so it is never offered as a searchable line. Operator
# codes used as a line name (HSB, MBB, SAB, FEX, SEV, P, ...) are real named
# services and are deliberately NOT in this set.
_BARE_CATEGORY_LINES = frozenset(
    {"RE", "RB", "S", "R", "RS", "E", "EX", "D", "IR", "IRE", "IC", "ICE", "EC", "ECE",
     "EN", "NJ", "BUS"}
)


def _is_bare_category_line(line: ServiceLine) -> bool:
    return (line.line or "").strip().upper() in _BARE_CATEGORY_LINES


def _line_endpoints(session: Session, line_ids: list[int]) -> dict[int, str]:
    """'First – Last' station of each line's longest route, for display only.

    Same axis `canonical_order` picks (the route_path with the most stops),
    so the endpoints shown in search match what the direction picker offers.
    """
    if not line_ids:
        return {}
    longest: dict[int, list[str]] = {}
    for line_id, route_path in session.execute(
        select(Train.line_id, Train.route_path).where(
            Train.line_id.in_(line_ids), Train.route_path.is_not(None)
        )
    ).all():
        names = [part for part in route_path.split("|") if part]
        if len(names) > len(longest.get(line_id, [])):
            longest[line_id] = names
    return {
        line_id: f"{names[0]} – {names[-1]}"
        for line_id, names in longest.items()
        if len(names) >= 2
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
    line_endpoints = _line_endpoints(session, wanted["line"])

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
            if line is None or _is_bare_category_line(line):
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
                    "stops": line_endpoints.get(line.id),
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
                    "stops": " – ".join(x for x in (train.origin_name, train.dest_name) if x)
                    or None,
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
    # The board is inherently "right now": resolve today's actual day type
    # (rather than defaulting to weekday) so its delay averages reflect
    # whether today is a holiday-timetable day at this station.
    is_holiday_flag = _is_holiday_flag(
        resolve_day_type(session, local_date(now).isoformat(), station=station)
        or DAY_TYPE_WEEKDAY
    )
    direction_cache: dict[int, dict[int, int]] = {}
    model = current_model()
    holiday_set = load_holiday_dates(session) if model is not None else None
    model_direction = _ModelDirectionCache(direction_cache)

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
        "model_stamp": model[0] if model is not None else None,
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
                select(StationStop, Train)
                .join(Train, Train.id == StationStop.train_id)
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

            for stop, train in rows:
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

                    direction = None
                    if train.line_id is not None:
                        dirs = direction_cache.get(train.line_id)
                        if dirs is None:
                            dirs = direction_by_train_id(session, train.line_id)
                            direction_cache[train.line_id] = dirs
                        direction = dirs.get(train.id)
                    trip_start_time = _train_trip_start_time(train)
                    sched_time = _sched_time_key(stop.planned_arrival_sec, stop.planned_departure_sec)
                    pred_kwargs = dict(
                        station_id=station_id,
                        line_id=train.line_id,
                        direction=direction,
                        trip_start_time=trip_start_time,
                        sched_time=sched_time,
                        is_holiday=is_holiday_flag,
                        train_id=train.id,
                    )

                    model_input = None
                    if model is not None:
                        model_input = _model_input(
                            session,
                            model[1],
                            train=train,
                            station_id=station_id,
                            service_date=service_date,
                            direction=model_direction.get(session, train),
                            holiday_set=holiday_set,
                            arr_sec=stop.planned_arrival_sec,
                            dep_sec=stop.planned_departure_sec,
                        )

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
                        "arr_pred": _delay_pred(
                            session,
                            side="arr",
                            **pred_kwargs,
                        ),
                        "dep_pred": _delay_pred(
                            session,
                            side="dep",
                            **pred_kwargs,
                        ),
                        "model_input": model_input,
                    }
                    calls[key] = entry

    ordered = sorted(calls.values(), key=lambda c: c["sort_key"])[:limit]
    payload["calls"] = ordered
    return payload


def train_route(
    session: Session, train_id: int, *, day_type: str = DAY_TYPE_WEEKDAY
) -> dict | None:
    """One train's whole journey, with scheduled times.

    The full route comes from train.route_path — IRIS's own ppth, every stop the
    service makes. Times come from station_stop, which exists only for the
    stations we actually poll, so most rows carry a name and nothing else.
    Showing the whole journey and marking which parts are measured is more
    honest than showing only the stops we happen to watch and letting it read
    like the entire route.
    """
    train = session.get(Train, train_id)
    if train is None:
        return None

    fv_id = tt_feed_version_id(session)
    is_holiday_flag = _is_holiday_flag(day_type)
    direction = (
        direction_by_train_id(session, train.line_id).get(train.id)
        if train.line_id is not None
        else None
    )
    trip_start_time = _train_trip_start_time(train)

    # This view shows a dateless pattern, not a specific run, so the model --
    # which needs an actual calendar date for its weekday/holiday features --
    # is asked about today, the same "typical day" the averages fall back to
    # via `day_type`'s own default.
    model = current_model()
    model_service_date = local_date(int(time.time()))
    holiday_set = load_holiday_dates(session) if model is not None else None
    if model is None:
        model_direction = None
    elif train.line_id is not None:
        # `direction` above already paid for direction_by_train_id on this
        # line; reuse it instead of asking _ModelDirectionCache to pay again
        # for a line this call only ever looks at once.
        model_direction = direction if direction is not None else UNKNOWN_DIRECTION
    else:
        model_direction = direction_for(session, train)

    measured: dict[int, tuple] = {}
    if fv_id is not None:
        # A numbered TT train can hold more than one station_stop variant --
        # e.g. a wing/coupled service some days, solo others (see
        # StationStop.path_sha1) -- and two variants can each use the same
        # stop_sequence for a genuinely different stop. Mixing rows from both
        # would show a route with two variants' stops interleaved, so this
        # picks ONE variant (whichever the train's most recent trip actually
        # ran as) and shows only that.
        variant = session.execute(
            select(Trip.path_sha1)
            .where(Trip.train_id == train_id, Trip.path_sha1.isnot(None))
            .order_by(Trip.service_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        stop_query = (
            select(StationStop, Station)
            .join(Station, Station.id == StationStop.station_id)
            .where(
                StationStop.train_id == train_id,
                StationStop.feed_version_id == fv_id,
            )
        )
        if variant is not None:
            stop_query = stop_query.where(StationStop.path_sha1 == variant)
        for stop, station in session.execute(
            # No trip row (or no digest) to prefer by: fall back to the
            # previous behaviour of a deterministic, if arbitrary, pick.
            stop_query.order_by(StationStop.id)
        ).all():
            measured[stop.stop_sequence] = (stop, station)

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
            "arr_pred": None,
            "dep_pred": None,
            "model_input": None,
        }
        hit = measured.get(index)
        if hit is not None:
            stop, station = hit
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
                    "arr_pred": _delay_pred(
                        session,
                        station_id=station.id,
                        line_id=train.line_id,
                        direction=direction,
                        trip_start_time=trip_start_time,
                        sched_time=_sched_time_key(
                            stop.planned_arrival_sec, stop.planned_departure_sec
                        ),
                        is_holiday=is_holiday_flag,
                        side="arr",
                        train_id=train.id,
                    ),
                    "dep_pred": _delay_pred(
                        session,
                        station_id=station.id,
                        line_id=train.line_id,
                        direction=direction,
                        trip_start_time=trip_start_time,
                        sched_time=_sched_time_key(
                            stop.planned_arrival_sec, stop.planned_departure_sec
                        ),
                        is_holiday=is_holiday_flag,
                        side="dep",
                        train_id=train.id,
                    ),
                    "model_input": (
                        _model_input(
                            session,
                            model[1],
                            train=train,
                            station_id=station.id,
                            service_date=model_service_date,
                            direction=model_direction,
                            holiday_set=holiday_set,
                            arr_sec=stop.planned_arrival_sec,
                            dep_sec=stop.planned_departure_sec,
                        )
                        if model is not None
                        else None
                    ),
                }
            )
        stops.append(entry)

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
        "model_stamp": model[0] if model is not None else None,
        "stops": stops,
    }


def _canonical_order(trains) -> list[str]:
    """The longest route on the line, taken as the axis every run travels along.

    Delegates to `dbrel.direction`, the module shared with the stop-average
    build (`derive/stop_averages.py`) so there is exactly one direction
    heuristic, not two that could drift.
    """
    return canonical_order(trains)


def _direction_of(train, index: dict[str, int]) -> int:
    """0 if the run travels the canonical way along the line, 1 if it runs back.

    See `dbrel.direction.direction_of` for the reasoning; kept as a thin alias
    here so existing call sites in this module don't need renaming.
    """
    return direction_of(train, index)


def _line_trains_by_direction(
    session: Session, line_id: int, *, limit: int = 300
) -> tuple[list[str], list[list[Train]]]:
    """Every numbered run on a line, split into its two directions of travel.

    Shared by every view that needs "the runs going this way": the line page, the
    direction/station pickers, and the around-this-time lookup all have to agree
    on which train goes which way, so the split lives here once.
    """
    return line_trains_by_direction(session, line_id, limit=limit)


def _direction_label(canonical: list[str], direction: int) -> str:
    if not canonical:
        return "all runs"
    return canonical[-1 if direction == 0 else 0]


def _directions_list(canonical: list[str], halves: list[list[Train]]) -> list[dict]:
    return [
        {"direction": direction, "label": _direction_label(canonical, direction),
         "n_runs": len(trains)}
        for direction, trains in enumerate(halves)
        if trains
    ]


def line_directions(session: Session, line_id: int) -> dict | None:
    """The line's two directions, for a picker to show once a line is chosen."""
    line = session.get(ServiceLine, line_id)
    if line is None:
        return None

    canonical, halves = _line_trains_by_direction(session, line_id)
    return {
        "line": {"id": line.id, "name": line.display_name or line.identity_key},
        "directions": _directions_list(canonical, halves),
    }


def corridor_directions(session: Session, train_id: int) -> dict | None:
    """line_directions for a line-less train's route (see corridor_trains_by_direction)."""
    grouped = corridor_trains_by_direction(session, train_id)
    if grouped is None:
        return None
    canonical, halves = grouped
    anchor = session.get(Train, train_id)
    return {
        "line": {"id": anchor.id, "name": anchor.display_name or f"#{anchor.id}"},
        "directions": _directions_list(canonical, halves),
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
    return _stations_in_travel_order(session, halves[direction])


def corridor_stations(session: Session, train_id: int, direction: int) -> list[dict] | None:
    """line_stations for a line-less train's route."""
    if direction not in (0, 1):
        return None
    grouped = corridor_trains_by_direction(session, train_id)
    if grouped is None:
        return None
    return _stations_in_travel_order(session, grouped[1][direction], main_variant_only=True)


def _on_main_variant(trains: list[Train]):
    """Keep only station_stop rows of each train's main route variant.

    A numbered long-distance train runs different routes on different days
    (ICE 1610 via Berlin some days, Frankfurt-Köln others), each stored as its
    own station_stop variant. A route grouped around a line-less train is
    built from each member's main route (`train.route_path` / `path_sha1`),
    so its stops and services must come from that same variant -- otherwise
    another day's detour drags in stations the route never serves.
    """
    main = {t.id: t.path_sha1 for t in trains}

    def keep(train_id: int, variant: str | None) -> bool:
        wanted = main.get(train_id)
        return wanted is None or variant == wanted

    return keep


def _stations_in_travel_order(
    session: Session, trains: list[Train], *, main_variant_only: bool = False
) -> list[dict]:
    """The body of line_stations, for any set of trains running one direction."""
    train_ids = [t.id for t in trains]
    fv_id = tt_feed_version_id(session)
    if not train_ids or fv_id is None:
        return []

    rows = session.execute(
        select(
            StationStop.train_id,
            StationStop.path_sha1,
            StationStop.station_id,
            StationStop.stop_sequence,
            Station.name,
        )
        .join(Station, Station.id == StationStop.station_id)
        .where(StationStop.train_id.in_(train_ids), StationStop.feed_version_id == fv_id)
    ).all()
    if main_variant_only:
        keep = _on_main_variant(trains)
        rows = [row for row in rows if keep(row[0], row[1])]
    if not rows:
        return []

    # A numbered TT train can hold more than one station_stop variant (a
    # wing/coupled service some days, solo others -- see StationStop.path_sha1),
    # sharing the same stop_sequence numbers for different physical stops. The
    # reference is picked as one (train, variant) pair -- whichever covers the
    # most stations -- so `position` is never built by interleaving two
    # variants' stop_sequences together.
    coverage: dict[tuple[int, str | None], int] = {}
    for train_id, variant, _station_id, _seq, _name in rows:
        key = (train_id, variant)
        coverage[key] = coverage.get(key, 0) + 1
    reference_id, reference_variant = max(coverage, key=coverage.get)

    position = {
        station_id: seq
        for train_id, variant, station_id, seq, _name in rows
        if train_id == reference_id and variant == reference_variant
    }
    names: dict[int, str] = {}
    for _train_id, _variant, station_id, _seq, name in rows:
        names.setdefault(station_id, name)

    # A station only a non-reference train serves (a short working's own extra
    # stop, say) has no position from the reference train -- keep it, just after
    # everything the reference train does cover, rather than dropping it.
    fallback = max(position.values(), default=0) + 1
    ordered_ids = sorted(names, key=lambda sid: position.get(sid, fallback))
    return [{"id": station_id, "name": names[station_id]} for station_id in ordered_ids]


def resolve_day_type(
    session: Session, service_date: str, *, station: Station | None = None
) -> str | None:
    """Which average pool a calendar date reads from -- WEEKDAY or SUNDAY_HOLIDAY.

    Checks the public_holiday table, not just the day of the week, so a date
    entered on the browse widget gets whichever pool the holiday table says it
    belongs to -- including a date that is a holiday for no reason a calendar
    rule would know about.

    Holidays are per-Bundesland now, so this needs to know *whose* holiday
    calendar to check:
    - `station` given (with a resolved `state`): that state's calendar.
    - `station` given but its `state` is NULL, or no `station` at all: only a
      date observed in all 16 states counts (the one case that's still
      decidable without knowing a state) -- see `holidays_de.is_holiday`.
    """
    from datetime import date as date_cls

    from dbrel.holidays_de import ALL_STATE_CODES

    try:
        day = date_cls.fromisoformat(service_date)
    except (ValueError, TypeError):
        return None

    holiday_set = load_holiday_dates(session)
    state = station.state if station is not None else None
    iso = day.isoformat()
    if day.weekday() == 6:
        is_hol = True
    elif state is not None:
        is_hol = (iso, state) in holiday_set
    else:
        is_hol = all((iso, code) in holiday_set for code in ALL_STATE_CODES)
    return DAY_TYPE_SUNDAY_HOLIDAY if is_hol else DAY_TYPE_WEEKDAY


def trips_around(
    session: Session,
    line_id: int,
    direction: int,
    station_id: int,
    time_of_day_s: int,
    *,
    day_type: str = DAY_TYPE_WEEKDAY,
    service_date: date_cls | None = None,
) -> dict | None:
    """The 2 scheduled calls at or before a time, and the 1 right after.

    The schedule is a recurring daily pattern -- that is the point of asking for
    a time of day rather than a timestamp -- so "before" and "after" wrap around
    midnight rather than running out at either end of the day. `day_type` picks
    which pool the model conditions its forecast on for each trip; it does not
    affect which trips are considered, since the underlying timetable pattern is
    itself dateless. Callers with an actual calendar date use resolve_day_type
    first, and pass that same date on as `service_date` for the model (which,
    unlike the averages, needs an actual date rather than a day-type bucket).
    """
    line = session.get(ServiceLine, line_id)
    station = session.get(Station, station_id)
    if line is None or station is None or direction not in (0, 1):
        return None

    _, halves = _line_trains_by_direction(session, line_id)
    header = {"id": line.id, "name": line.display_name or line.identity_key}
    return _trips_around_in(
        session, header, halves[direction], direction, station, time_of_day_s,
        day_type=day_type, line_id=line_id, service_date=service_date,
    )


def corridor_trips_around(
    session: Session,
    train_id: int,
    direction: int,
    station_id: int,
    time_of_day_s: int,
    *,
    day_type: str = DAY_TYPE_WEEKDAY,
    service_date: date_cls | None = None,
) -> dict | None:
    """trips_around for a line-less train's route; averages come per train."""
    station = session.get(Station, station_id)
    if station is None or direction not in (0, 1):
        return None
    grouped = corridor_trains_by_direction(session, train_id)
    if grouped is None:
        return None
    anchor = session.get(Train, train_id)
    header = {"id": anchor.id, "name": anchor.display_name or f"#{anchor.id}"}
    return _trips_around_in(
        session, header, grouped[1][direction], direction, station, time_of_day_s,
        day_type=day_type, line_id=None, service_date=service_date,
    )


def _trips_around_in(
    session: Session,
    header: dict,
    trains: list[Train],
    direction: int,
    station: Station,
    time_of_day_s: int,
    *,
    day_type: str,
    line_id: int | None,
    service_date: date_cls | None = None,
) -> dict:
    """The body of trips_around, for any set of trains running one direction.

    `line_id` None means line-less trains: their averages are looked up per
    train (train_stop_delay_average) rather than per line and direction.
    """
    station_id = station.id
    trains_by_id = {t.id: t for t in trains}
    fv_id = tt_feed_version_id(session)
    model = current_model()
    model_service_date = service_date or local_date(int(time.time()))
    holiday_set = load_holiday_dates(session) if model is not None else None
    model_direction = _ModelDirectionCache()

    payload = {
        "line": header,
        "direction": direction,
        "station": {"id": station.id, "name": station.name},
        "model_stamp": model[0] if model is not None else None,
        "trips": [],
    }
    if not trains_by_id or fv_id is None:
        return payload

    rows = session.execute(
        select(StationStop)
        .where(
            StationStop.station_id == station_id,
            StationStop.train_id.in_(list(trains_by_id)),
            StationStop.feed_version_id == fv_id,
        )
    ).all()
    if line_id is None:
        keep = _on_main_variant(trains)
        rows = [(stop,) for (stop,) in rows if keep(stop.train_id, stop.path_sha1)]

    candidates = []
    for (stop,) in rows:
        sched = stop.planned_departure_sec
        if sched is None:
            sched = stop.planned_arrival_sec
        candidates.append((sched % 86400, stop))
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
    picked: list[tuple] = []
    for i, relation in picks:
        _tod, stop = candidates[i]
        if stop.id in seen:
            continue
        seen.add(stop.id)
        picked.append((stop, relation))

    is_holiday_flag = _is_holiday_flag(day_type)
    trips = []
    for stop, relation in picked:
        train = trains_by_id[stop.train_id]
        trip_start_time = _train_trip_start_time(train)
        pred_kwargs = dict(
            station_id=station_id,
            line_id=line_id,
            direction=direction,
            trip_start_time=trip_start_time,
            sched_time=_sched_time_key(stop.planned_arrival_sec, stop.planned_departure_sec),
            is_holiday=is_holiday_flag,
            train_id=train.id,
        )

        model_input = None
        if model is not None:
            model_input = _model_input(
                session,
                model[1],
                train=train,
                station_id=station_id,
                service_date=model_service_date,
                direction=model_direction.get(session, train),
                holiday_set=holiday_set,
                arr_sec=stop.planned_arrival_sec,
                dep_sec=stop.planned_departure_sec,
            )

        trips.append(
            {
                "train_id": train.id,
                "name": train.display_name or f"#{train.id}",
                "url": f"/train/{train.id}/",
                "sched_arr": _hhmm(stop.planned_arrival_sec),
                "sched_dep": _hhmm(stop.planned_departure_sec),
                "arr_pred": _delay_pred(session, side="arr", **pred_kwargs),
                "dep_pred": _delay_pred(session, side="dep", **pred_kwargs),
                "model_input": model_input,
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
    is_holiday_flag = _is_holiday_flag(day_type)
    line = session.get(ServiceLine, line_id)
    if line is None:
        return None

    # Dateless, like train_route: a per-run figure has no specific service date
    # to ask the model about, so it is asked about today.
    model = current_model()
    model_service_date = local_date(int(time.time()))
    holiday_set = load_holiday_dates(session) if model is not None else None
    model_direction = _ModelDirectionCache()

    fv_id = tt_feed_version_id(session)
    monitored = (
        dict(
            session.execute(
                # DISTINCT station_id, not a row count: a train with more than
                # one station_stop variant (wing/coupled some days, solo
                # others -- StationStop.path_sha1) would otherwise have its
                # monitored-station count added up across variants instead of
                # reflecting distinct stations actually covered.
                select(StationStop.train_id, func.count(func.distinct(StationStop.station_id)))
                .join(Train, Train.id == StationStop.train_id)
                .where(Train.line_id == line_id, StationStop.feed_version_id == fv_id)
                .group_by(StationStop.train_id)
            ).all()
        )
        if fv_id is not None
        else {}
    )

    canonical, halves = _line_trains_by_direction(session, line_id, limit=limit)

    # Train.origin_station_id is unreliable -- NULL even for trains whose
    # origin is a fully tracked station with its own station_stop row (a
    # backfill/ingest gap, not a "genuinely unmonitored origin" signal). The
    # earliest monitored stop_sequence per train is the real origin station.
    origin_station_by_train: dict[int, int] = {}
    if fv_id is not None:
        all_train_ids = [t.id for trains in halves for t in trains]
        if all_train_ids:
            for train_id, station_id in session.execute(
                select(StationStop.train_id, StationStop.station_id)
                .where(
                    StationStop.train_id.in_(all_train_ids),
                    StationStop.feed_version_id == fv_id,
                )
                .order_by(StationStop.train_id, StationStop.stop_sequence)
            ).all():
                origin_station_by_train.setdefault(train_id, station_id)

    result_halves: list[list[dict]] = [[], []]
    for direction, trains in enumerate(halves):
        for t in trains:
            # A run's own "typical delay" is its historical departure delay
            # from its own origin station -- the only station a per-run (not
            # per-stop) figure can unambiguously mean.
            trip_start_time = _train_trip_start_time(t)
            origin_station_id = origin_station_by_train.get(t.id)

            model_input = None
            if model is not None:
                model_input = _model_input(
                    session,
                    model[1],
                    train=t,
                    station_id=origin_station_id,
                    service_date=model_service_date,
                    direction=model_direction.get(session, t),
                    holiday_set=holiday_set,
                    arr_sec=None,
                    dep_sec=t.first_departure_sec,
                )

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
                    "pred": _delay_pred(
                        session,
                        station_id=origin_station_id,
                        line_id=line_id,
                        direction=direction,
                        trip_start_time=trip_start_time,
                        sched_time=trip_start_time,
                        is_holiday=is_holiday_flag,
                        side="dep",
                    ),
                    "model_input": model_input,
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
        "model_stamp": model[0] if model is not None else None,
        "directions": [
            {
                "label": _direction_label(canonical, direction),
                "n_runs": len(runs),
                "runs": runs,
            }
            for direction, runs in enumerate(result_halves)
        ],
    }
