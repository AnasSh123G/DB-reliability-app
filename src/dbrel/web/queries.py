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
from types import SimpleNamespace

from sqlalchemy import func, inspect, select, text
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
from dbrel.deployed import TRAIN_VARIANT_RUN
from dbrel.direction import (
    canonical_order,
    corridor_trains_by_direction,
    direction_by_train_id,
    direction_of,
    line_trains_by_direction,
    train_direction_map,
)
from dbrel.holidays_de import load_holiday_dates
from dbrel.derive.passenger_lines import BARE_CATEGORY_LINES, is_bare_category_line
from dbrel.identity import prefix_range, search_norm
from dbrel.ml.arrival import ARRIVAL_KIND, arrival_inputs
from dbrel.ml.notice_rates import add_serving_rates, serving_rates, uses_notice_rates
from dbrel.ml.bakeoff import MANIFEST_NAME, current_artifact
from dbrel.ml.features import UNKNOWN_DIRECTION, compute_features, direction_for, encode_categoricals
from dbrel.ml.nn import model_inputs
from dbrel.ml.timetable import calls_at
from dbrel.models import (
    DAY_TYPE_SUNDAY_HOLIDAY,
    DAY_TYPE_WEEKDAY,
    FeedVersion,
    SearchAlias,
    PassengerLine,
    PassengerLineMember,
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


def warm_model(engine) -> None:
    """Load what the current model needs from the database (its notice rates), so the first
    page after a server start does not wait for it. A no-op for a model without rate inputs."""
    model = current_model()
    if model is not None and model[1].get("kind") == ARRIVAL_KIND and uses_notice_rates(model[1]):
        serving_rates(model[1], engine)


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
    variant: str | None = None,
) -> dict | None:
    """This call's encoded model input, or None.

    `variant` is the route variant (StationStop.path_sha1) the page shows this
    call from, so the input describes that same call.

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
    calls = calls_at(session, train, station_id, service_date, variant=variant)
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
    arrival_only = manifest.get("kind") == ARRIVAL_KIND
    if arrival_only and call.planned_arrival_sec is None:
        return None  # an arrival-only model has nothing to say about an origin call
    raw = call.raw(direction=direction, holiday_set=holiday_set, arrival_clock=arrival_only)
    features = encode_categoricals(compute_features(raw), manifest["vocab"])
    if arrival_only:
        features = add_serving_rates(features, raw, manifest, session.get_bind())
        cats, nums = arrival_inputs(features, manifest["prep"])
    else:
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

def _is_bare_category_line(line: ServiceLine) -> bool:
    return is_bare_category_line(line.line)


def passenger_name(session: Session, train: Train) -> str:
    """The name a passenger sees on the platform display.

    Regional and S-Bahn trains are signed by their line ("RB22", "S1"), not
    their operational number ("RB 10478"), which is internal. Long-distance
    trains really are signed by number ("ICE 599"), and a line that is just a
    bare category ("RB") says nothing, so both keep the number.
    """
    number_name = train.display_name or f"#{train.id}"
    if train.segment == "FV" or train.line_id is None:
        return number_name
    line = session.get(ServiceLine, train.line_id)
    if line is None or not line.display_name or _is_bare_category_line(line):
        return number_name
    return line.display_name


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


# The Bundesländer a search can be narrowed to, as station.state holds them (ISO 3166-2:DE).
STATES = ("DE-BB", "DE-BE", "DE-BW", "DE-BY", "DE-HB", "DE-HE", "DE-HH", "DE-MV",
          "DE-NI", "DE-NW", "DE-RP", "DE-SH", "DE-SL", "DE-SN", "DE-ST", "DE-TH")


def _stops_in_state(session: Session, train_id, state: str):
    """SQL: the train `train_id` has a scheduled stop at a station in `state`.

    Args:
        session: the database session, for the timetable feed's id.
        train_id: a column (or value) holding the train's id; the EXISTS is correlated on it, so
            SQLite looks the train's own stops up by its id instead of scanning a state's stations.
        state: a Bundesland code from STATES.

    Returns:
        An EXISTS clause, true when the train calls at any station of `state`.
    """
    return (
        select(StationStop.train_id)
        .join(Station, Station.id == StationStop.station_id)
        .where(
            StationStop.train_id == train_id,
            StationStop.feed_version_id == tt_feed_version_id(session),
            Station.state == state,
        )
        .exists()
    )


def _lines_in_state(session: Session, line_ids: list[int], state: str, *, passenger: bool) -> set[int]:
    """The lines among `line_ids` with a running train that stops in `state`.

    Args:
        session: the database session.
        line_ids: passenger_line ids (`passenger` True) or service_line ids (False).
        state: a Bundesland code from STATES.
        passenger: which kind of line `line_ids` are.

    Returns:
        The ids of the lines that call somewhere in `state`.
    """
    if not line_ids:
        return set()
    cutoff = _active_cutoff(session)
    running = () if cutoff is None else (Train.last_seen_date.is_(None) | (Train.last_seen_date >= cutoff),)
    if passenger:
        line, member = PassengerLine.id, (
            select(PassengerLineMember.train_id)
            .join(Train, Train.id == PassengerLineMember.train_id)
            .where(PassengerLineMember.passenger_line_id == PassengerLine.id, *running,
                   _stops_in_state(session, PassengerLineMember.train_id, state))
        )
    else:
        line, member = ServiceLine.id, (
            select(Train.id).where(Train.line_id == ServiceLine.id, *running,
                                   _stops_in_state(session, Train.id, state))
        )
    return set(session.execute(select(line).where(line.in_(line_ids), member.exists())).scalars())


def _passenger_line_results(
    session: Session, service_line_ids: list[int], *, exact_line_ids: set[int], state: str | None = None
) -> list[dict]:
    """Search results for the passenger lines behind matched service_line rows.

    Exact name matches first, trains before replacement buses, then the
    busiest line first -- "RE4" should open on the RE4 most people mean.
    With `state`, only the lines that stop somewhere in that Bundesland.
    """
    bare = {
        line.id
        for line in session.execute(
            select(ServiceLine).where(ServiceLine.id.in_(service_line_ids))
        ).scalars()
        if _is_bare_category_line(line)
    }
    wanted = [i for i in service_line_ids if i not in bare]
    if not wanted:
        return []
    cutoff = _active_cutoff(session)
    still_running = (
        ()
        if cutoff is None
        else (Train.last_seen_date.is_(None) | (Train.last_seen_date >= cutoff),)
    )
    via: dict[int, set[int]] = {}
    for service_line_id, passenger_line_id in session.execute(
        select(Train.line_id, PassengerLineMember.passenger_line_id)
        .join(PassengerLineMember, PassengerLineMember.train_id == Train.id)
        .where(Train.line_id.in_(wanted), *still_running)
        .distinct()
    ).all():
        via.setdefault(passenger_line_id, set()).add(service_line_id)
    if state is not None:
        in_state = _lines_in_state(session, list(via), state, passenger=True)
        via = {line_id: found for line_id, found in via.items() if line_id in in_state}
    if not via:
        return []
    runs = dict(
        session.execute(
            select(PassengerLineMember.passenger_line_id, func.count())
            .join(Train, Train.id == PassengerLineMember.train_id)
            .where(PassengerLineMember.passenger_line_id.in_(list(via)), *still_running)
            .group_by(PassengerLineMember.passenger_line_id)
        ).all()
    )
    lines = session.execute(select(PassengerLine).where(PassengerLine.id.in_(list(via)))).scalars()
    out = []
    for line in lines:
        n = runs.get(line.id, 0)
        # IRIS publishes rail-replacement buses under the line they replace,
        # so the same label can name both. Saying which is the difference
        # between a useful result and a baffling duplicate.
        is_bus = (line.category or "").upper() == "BUS"
        out.append(
            {
                "kind": "line",
                "rank": _KIND_RANK["line"],
                "exact": bool(via[line.id] & exact_line_ids),
                "id": line.id,
                "name": line.name,
                "sub": (
                    f"Replacement bus · {n} run{'s' if n != 1 else ''}"
                    if is_bus
                    else f"Line · {n} run{'s' if n != 1 else ''}"
                ),
                "replacement_bus": is_bus,
                "stops": line.endpoints,
                "url": f"/line/{line.id}/",
                "_runs": n,
            }
        )
    out.sort(key=lambda r: (not r["exact"], r["replacement_bus"], -r["_runs"]))
    for r in out:
        del r["_runs"]
    return out


# Line numbers stop at two digits (RB22, S41); three or more means the user
# is typing a train's own number, so its individual runs are worth listing.
_TRAIN_NUMBER_MIN_DIGITS = 3


def suggest(
    session: Session, term: str, *, limit: int = 8, only: str | None = None, state: str | None = None
) -> list[dict]:
    """Stations, lines and trains whose name starts with `term`.

    One prefix scan serves all three: search_alias already holds a row per
    searchable form of every entity, and ix_alias_lookup turns the half-open
    range into a seek. `only` narrows the scan to one kind, for a picker that
    only ever wants lines, say, rather than filtering the general result client
    side and hoping a line survives the top `limit` matches. `state` (a code
    from STATES) keeps only the lines and trains that stop somewhere in that
    Bundesland; stations are offered from everywhere, their names being unique.
    """
    normalised = search_norm(term or "")
    if len(normalised) < MIN_QUERY_LEN:
        return []
    lo, hi = prefix_range(normalised)

    conditions = [SearchAlias.alias_norm >= lo, SearchAlias.alias_norm < hi]
    if only is not None:
        conditions.append(_SUGGEST_KIND_COLUMN[only].is_not(None))
    number_search = sum(ch.isdigit() for ch in normalised) >= _TRAIN_NUMBER_MIN_DIGITS
    if not number_search:
        # Not (yet) a train-number search: a run named by its line ("RB22",
        # see passenger_name) would only repeat that line once per run, so it
        # is left to the line's own result. An exact number still finds it.
        # Asked of the alias's own train (EXISTS on its id), never as a list of all trains: in a
        # deployed database `train` is a view, and a list or a LEFT JOIN makes SQLite build the
        # whole view on every keystroke (2026-10-07: ~480 ms per search, 0.3 ms this way).
        signed_by_line = (
            select(Train.id)
            .join(ServiceLine, ServiceLine.id == Train.line_id)
            .where(
                Train.id == SearchAlias.train_id,
                func.coalesce(Train.segment, "") != "FV",
                func.upper(func.trim(func.coalesce(ServiceLine.line, ""))).not_in(
                    BARE_CATEGORY_LINES
                ),
            )
        )
        conditions.append(
            (SearchAlias.alias_norm == normalised)
            | SearchAlias.train_id.is_(None)
            | ~signed_by_line.exists()
        )

    cutoff = _active_cutoff(session)
    if cutoff is not None:
        # A retired train is not offered; filtered here, before the limit, so retired matches
        # cannot take places from running ones. An alias whose train has no row, or no
        # last_seen_date, stays (as the LEFT JOIN this replaces kept it).
        retired = select(Train.id).where(Train.id == SearchAlias.train_id, Train.last_seen_date < cutoff)
        conditions.append(SearchAlias.train_id.is_(None) | ~retired.exists())
    if state is not None:
        # Before the limit too, so trains elsewhere cannot take the places of this state's.
        conditions.append(SearchAlias.train_id.is_(None) | _stops_in_state(session, SearchAlias.train_id, state))

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
            # "rb" means a line far more often than one of thousands of runs.
            *(() if number_search else (SearchAlias.line_id.is_(None),)),
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

    passenger_mode = _passenger_lines_ready(session)
    if state is not None and not passenger_mode:
        # Without passenger lines a matched service line is the result itself, so it is
        # checked here; with them, _passenger_line_results checks each passenger line.
        line_ids = [ident for kind, ident in order if kind == "line"]
        in_state = _lines_in_state(session, line_ids, state, passenger=False)
        order = [(kind, ident) for kind, ident in order if kind != "line" or ident in in_state]

    wanted: dict[str, list[int]] = {"station": [], "line": [], "train": []}
    for kind, ident in order:
        wanted[kind].append(ident)

    stations = {
        row.id: row
        for row in session.execute(
            select(Station).where(Station.id.in_(wanted["station"]))
        ).scalars()
    } if wanted["station"] else {}
    # With passenger lines built, matched service lines are only expanded into passenger lines
    # (which carry their own endpoints and run counts), so their own rows, run counts and
    # endpoints are looked up only without them (2026-10-07: ~110 ms of a 200 ms "bus" search).
    service_lines = wanted["line"] if not passenger_mode else []
    lines = {
        row.id: row
        for row in session.execute(
            select(ServiceLine).where(ServiceLine.id.in_(service_lines))
        ).scalars()
    } if service_lines else {}
    trains = {
        row.id: row
        for row in session.execute(
            select(Train).where(Train.id.in_(wanted["train"]))
        ).scalars()
    } if wanted["train"] else {}

    # How many numbered runs each matched line has, so the result can say so.
    run_counts: dict[int, int] = {}
    if service_lines:
        run_counts = dict(
            session.execute(
                select(Train.line_id, func.count(Train.id))
                .where(Train.line_id.in_(service_lines))
                .group_by(Train.line_id)
            ).all()
        )
    line_endpoints = _line_endpoints(session, service_lines)

    # With passenger lines built, a matched service_line stands for every
    # passenger line its trains belong to (one "RE/4" row covers RE4s all over
    # Germany), each listed once and only while it still runs.
    passenger_results: list[dict] | None = None
    if wanted["line"] and passenger_mode:
        passenger_results = _passenger_line_results(
            session,
            [ident for kind, ident in order if kind == "line"],
            exact_line_ids={ident for kind, ident in exact if kind == "line"},
            state=state,
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
        elif kind == "line" and passenger_results is not None:
            # All of them at the first line match's position, then never again.
            results.extend(passenger_results)
            passenger_results = []
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
                    "name": passenger_name(session, train),
                    "number_name": train.display_name or f"#{train.id}",
                    "sub": f"Train · {journey}" if journey else "Train",
                    "stops": " – ".join(x for x in (train.origin_name, train.dest_name) if x)
                    or None,
                    "url": f"/train/{train.id}/",
                }
            )

    results.sort(key=lambda r: (not r["exact"], r["rank"]))
    # One number can be stored as many trains (an operator disagreement parks
    # each route variant on its own identity, see ingest/runs.py), which would
    # list e.g. "ICE 100 · Interlaken Ost – Dortmund Hbf" dozens of times.
    unique: list[dict] = []
    shown: set[tuple] = set()
    for r in results:
        key = (r["kind"], r["name"], r.get("number_name"), r.get("stops"))
        if key in shown:
            continue
        shown.add(key)
        r.pop("exact", None)
        r.pop("rank", None)
        unique.append(r)
    return unique[:limit]


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
    active_cutoff = _active_cutoff(session)
    # One variant per train, or one trip is listed once per variant. Filled per
    # batch for just the trains in it: a hub has thousands of trains, the board
    # shows a few dozen.
    variant_of: dict[int, str | None] = {}

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
                    # A retired train (see ACTIVE_WINDOW_DAYS) is not on today's board.
                    *(
                        ()
                        if active_cutoff is None
                        else (
                            Train.last_seen_date.is_(None)
                            | (Train.last_seen_date >= active_cutoff),
                        )
                    ),
                )
                # id breaks ties: without it, which of several same-time rows survive the limit
                # depends on the query plan, so two copies of the data could show different boards.
                .order_by(column, StationStop.id)
                .limit(limit)
            ).all()

            unseen = {stop.train_id for stop, _train in rows} - variant_of.keys()
            if unseen:
                variant_of.update(_variant_per_train_at(session, station_id, fv_id, unseen))
            for stop, train in rows:
                if stop.path_sha1 != variant_of.get(stop.train_id):
                    continue
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
                            variant=stop.path_sha1,
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
                        "train": passenger_name(session, train),
                        "number_name": train.display_name or train.search_name or f"#{train.id}",
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
    session: Session, train_id: int, *, day_type: str = DAY_TYPE_WEEKDAY, station_id: int | None = None
) -> dict | None:
    """One train's journey: the stops it has scheduled times at.

    `station_id` is the stop the caller came from (a board row, a search result). The route shown
    is then the variant a board shows for that stop -- one that calls there -- so the page never
    drops the stop that led to it.

    Only stops with a station_stop row (in the one route variant chosen below)
    are returned -- a stop with no times reads as noise to a passenger. The
    bare ppth (train.route_path) is used only when no stop has times at all,
    so the page still has something to show.
    """
    train = session.get(Train, train_id)
    if train is None:
        return None
    if not _is_active(train, _active_cutoff(session)):
        # Retired (not in the timetable for ACTIVE_WINDOW_DAYS): its last route no longer runs, so
        # the page says so instead of showing it.
        model = current_model()
        return {
            "train": _route_header(session, train, 0),
            "retired": {"last_seen_date": train.last_seen_date},
            "n_monitored": 0,
            "model_stamp": model[0] if model is not None else None,
            "stops": [],
        }

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
    variant = None
    if fv_id is not None:
        # A numbered TT train can hold more than one station_stop variant --
        # e.g. a wing/coupled service some days, solo others (see
        # StationStop.path_sha1) -- and two variants can each use the same
        # stop_sequence for a genuinely different stop. Mixing rows from both
        # would show a route with two variants' stops interleaved, so this
        # picks ONE variant (whichever the train's most recent trip actually
        # ran as) and shows only that.
        variant = (
            _variant_per_train_at(session, station_id, fv_id, {train_id}).get(train_id)
            if station_id is not None
            else None
        )
        if variant is None:
            # The variant of the most recent run (a train runs at most once per date).
            recent = [
                (last, v) for (_t, v), (last, _n) in _variant_runs(session, [train_id]).items()
                if v is not None
            ]
            variant = max(recent)[1] if recent else None

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

    names = [part for part in (train.route_path or "").split("|") if part]
    # stop_sequence is the 1-based index into the ppth of ITS OWN variant, and
    # route_path is the ppth of train.path_sha1's variant only. Reading another
    # variant's sequence numbers against it puts times on the wrong station
    # names, so route_path names are trusted only when the variants match;
    # otherwise the station row's own name is used.
    names_align = variant is None or variant == train.path_sha1
    if measured:
        sequence = sorted(measured)
    else:
        sequence = list(range(1, len(names) + 1))

    stops: list[dict] = []
    for index in sequence:
        hit = measured.get(index)
        if hit is not None and not (names_align and index <= len(names)):
            name = hit[1].name
        else:
            name = names[index - 1]
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
                            variant=stop.path_sha1,
                        )
                        if model is not None
                        else None
                    ),
                }
            )
        stops.append(entry)

    return {
        "train": _route_header(session, train, len(stops)),
        "n_monitored": sum(1 for s in stops if s["monitored"]),
        "model_stamp": model[0] if model is not None else None,
        "stops": stops,
    }


def _route_header(session: Session, train: Train, n_stops: int) -> dict:
    """The `train` part of a train route response.

    Args:
        session: the database session.
        train: the train the page is about.
        n_stops: how many stops the response lists.

    Returns:
        Its names, category, operator, ends, stop count and line.
    """
    line_id = public_line_id(session, train)
    line = _line_header(session, line_id) if line_id is not None else None
    return {
        "id": train.id,
        "name": passenger_name(session, train),
        "number_name": train.display_name or f"#{train.id}",
        "category": train.category,
        "segment": train.segment,
        "number": train.train_number,
        "operator": train.operator,
        "origin": train.origin_name,
        "destination": train.dest_name,
        "n_stops": n_stops,
        "line": {"id": line["id"], "name": line["name"]} if line else None,
        "replacement_bus": (train.category or "").upper() == "BUS",
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


# A train not seen running for this long is taken to be out of the timetable
# (the December timetable change retires thousands of numbers at once), so the
# pages stop offering it for dates it will not run on.
ACTIVE_WINDOW_DAYS = 14
_ACTIVE_CUTOFF_TTL_S = 600
# (engine, computed at, cutoff): one cached value per database, ~40 ms to compute.
_active_cutoff_cache: tuple[object, float, str | None] | None = None


def _active_cutoff(session: Session) -> str | None:
    """The oldest last_seen_date that still counts as running, 'YYYY-MM-DD'.

    Measured back from the newest data rather than from today, so a paused
    collector does not make every train look retired.
    """
    global _active_cutoff_cache
    now = time.time()
    engine = session.get_bind()
    if (
        _active_cutoff_cache is not None
        and _active_cutoff_cache[0] is engine
        and now - _active_cutoff_cache[1] < _ACTIVE_CUTOFF_TTL_S
    ):
        return _active_cutoff_cache[2]
    newest = session.execute(select(func.max(Train.last_seen_date))).scalar_one_or_none()
    cutoff = None
    if newest:
        cutoff = (date_cls.fromisoformat(newest) - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat()
    _active_cutoff_cache = (engine, now, cutoff)
    return cutoff


def _is_active(train: Train, cutoff: str | None) -> bool:
    return cutoff is None or train.last_seen_date is None or train.last_seen_date >= cutoff


def _only_active(session: Session, halves: list[list[Train]]) -> list[list[Train]]:
    cutoff = _active_cutoff(session)
    return [[t for t in half if _is_active(t, cutoff)] for half in halves]


_passenger_lines_cache: tuple[object, float, bool] | None = None


def _passenger_lines_ready(session: Session) -> bool:
    """Whether build-passenger-lines has run, so public line ids are
    passenger_line ids. Until then they stay service_line ids, as before."""
    global _passenger_lines_cache
    now = time.time()
    engine = session.get_bind()
    if (
        _passenger_lines_cache is not None
        and _passenger_lines_cache[0] is engine
        and now - _passenger_lines_cache[1] < _ACTIVE_CUTOFF_TTL_S
    ):
        return _passenger_lines_cache[2]
    has_table = session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='passenger_line'")
    ).first()
    ready = bool(has_table) and session.execute(select(PassengerLine.id).limit(1)).first() is not None
    _passenger_lines_cache = (engine, now, ready)
    return ready


def _line_header(session: Session, line_id: int) -> dict | None:
    """A public line id's name and kind, for a page header."""
    if _passenger_lines_ready(session):
        line = session.get(PassengerLine, line_id)
        if line is None:
            return None
        return {
            "id": line.id,
            "name": line.name,
            "category": line.category,
            "operator": None,
            "replacement_bus": (line.category or "").upper() == "BUS",
        }
    line = session.get(ServiceLine, line_id)
    if line is None:
        return None
    return {
        "id": line.id,
        "name": line.display_name or line.identity_key,
        "category": line.category,
        "operator": line.operator,
        "replacement_bus": (line.category or "").upper() == "BUS",
    }


def public_line_id(session: Session, train: Train) -> int | None:
    """The line a train's pages link to: its passenger line once built."""
    if _passenger_lines_ready(session):
        return session.execute(
            select(PassengerLineMember.passenger_line_id).where(
                PassengerLineMember.train_id == train.id
            )
        ).scalar_one_or_none()
    return train.line_id


def _split_by_direction(trains: list[Train]) -> tuple[list[str], list[list[Train]]]:
    """canonical axis + the two directions, for one passenger line's trains.

    Clusters distinct routes rather than trains: a busy line has thousands of
    trains but a few dozen routes, and _connected_components is pairwise.
    """
    reps: dict[tuple, SimpleNamespace] = {}
    rep_of: dict[int, tuple] = {}
    for t in trains:
        key = (t.route_path, t.origin_name, t.dest_name)
        if key not in reps:
            reps[key] = SimpleNamespace(
                id=len(reps), route_path=t.route_path, origin_name=t.origin_name,
                dest_name=t.dest_name,
            )
        rep_of[t.id] = key
    rep_direction = train_direction_map(list(reps.values()))
    canonical = canonical_order(list(reps.values()))
    halves: list[list[Train]] = [[], []]
    for t in trains:
        halves[rep_direction[reps[rep_of[t.id]].id]].append(t)
    return canonical, halves


def _line_trains_by_direction(
    session: Session, line_id: int, *, limit: int | None = None
) -> tuple[list[str], list[list[Train]]]:
    """Every run on a line still running, split into its two directions of travel.

    Shared by every view that needs "the runs going this way": the line page, the
    direction/station pickers, and the around-this-time lookup all have to agree
    on which train goes which way, so the split lives here once. Retired runs
    are dropped (ACTIVE_WINDOW_DAYS).

    With passenger lines built, `line_id` is a passenger_line id and, unless a
    `limit` is given, every still-running member counts: a busy line's runs
    past the first 300 are the ones a passenger asking about the evening needs.
    Only the line page caps, since it builds a model input per run. Otherwise
    it is a service_line id, split as before.
    """
    if not _passenger_lines_ready(session):
        canonical, halves = line_trains_by_direction(session, line_id, limit=limit or 300)
        return canonical, _only_active(session, halves)
    cutoff = _active_cutoff(session)
    trains = session.execute(
        select(Train)
        .join(PassengerLineMember, PassengerLineMember.train_id == Train.id)
        .where(PassengerLineMember.passenger_line_id == line_id)
        .order_by(Train.first_departure_sec, Train.train_number)
    ).scalars().all()
    trains = [t for t in trains if _is_active(t, cutoff)]
    canonical, halves = _split_by_direction(trains)
    if limit is not None and len(trains) > limit:
        # Split first, then cap: the direction split must see the whole line.
        kept = {t.id for t in trains[:limit]}
        halves = [[t for t in half if t.id in kept] for half in halves]
    return canonical, halves


class _AveragesDirection:
    """A train's direction as the averages tables key it: per service_line,
    which is not the passenger line a page is showing."""

    def __init__(self, line_direction_cache: dict[int, dict[int, int]] | None = None) -> None:
        # Share the dict with the call site's _ModelDirectionCache: both need
        # direction_by_train_id per service_line, which costs seconds on a
        # national line like "S/1", so neither may compute it twice.
        self._by_line: dict[int, dict[int, int]] = (
            line_direction_cache if line_direction_cache is not None else {}
        )

    def get(self, session: Session, train: Train) -> int | None:
        if train.line_id is None:
            return None
        dirs = self._by_line.get(train.line_id)
        if dirs is None:
            dirs = direction_by_train_id(session, train.line_id)
            self._by_line[train.line_id] = dirs
        return dirs.get(train.id)


def _corridor_trains_by_direction(
    session: Session, train_id: int
) -> tuple[list[str], list[list[Train]]] | None:
    """corridor_trains_by_direction, keeping only runs still running."""
    grouped = corridor_trains_by_direction(session, train_id)
    if grouped is None:
        return None
    canonical, halves = grouped
    return canonical, _only_active(session, halves)


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
    header = _line_header(session, line_id)
    if header is None:
        return None

    canonical, halves = _line_trains_by_direction(session, line_id)
    return {
        "line": {"id": header["id"], "name": header["name"]},
        "directions": _directions_list(canonical, halves),
    }


def corridor_directions(session: Session, train_id: int) -> dict | None:
    """line_directions for a line-less train's route (see corridor_trains_by_direction)."""
    grouped = _corridor_trains_by_direction(session, train_id)
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
    if _line_header(session, line_id) is None or direction not in (0, 1):
        return None

    _, halves = _line_trains_by_direction(session, line_id)
    return _stations_in_travel_order(session, halves[direction])


def corridor_stations(session: Session, train_id: int, direction: int) -> list[dict] | None:
    """line_stations for a line-less train's route."""
    if direction not in (0, 1):
        return None
    grouped = _corridor_trains_by_direction(session, train_id)
    if grouped is None:
        return None
    return _stations_in_travel_order(session, grouped[1][direction], main_variant_only=True)


# Per engine: whether it is a deployed database with the run summary (see dbrel.deployed).
_run_summary_present: dict = {}


def _variant_runs(session: Session, train_ids) -> dict[tuple[int, str | None], tuple[str, int]]:
    """(train_id, variant) -> (last service date, number of runs), over every recorded run.

    A deployed database holds these as `train_variant_run`; the collection database has the runs
    themselves in `trip`. Both give the same numbers, so callers never know which they read.
    """
    engine = session.get_bind()
    if engine not in _run_summary_present:
        # Through the session's own connection: a second one could end its transaction.
        _run_summary_present[engine] = inspect(session.connection()).has_table(TRAIN_VARIANT_RUN.name)
    summary = TRAIN_VARIANT_RUN.c
    ids = list(train_ids)
    out: dict[tuple[int, str | None], tuple[str, int]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        if _run_summary_present[engine]:
            query = select(summary.train_id, summary.path_sha1, summary.last_service_date, summary.n_runs).where(
                summary.train_id.in_(chunk)
            )
        else:
            query = (
                select(Trip.train_id, Trip.path_sha1, func.max(Trip.service_date), func.count())
                .where(Trip.train_id.in_(chunk))
                .group_by(Trip.train_id, Trip.path_sha1)
            )
        for train_id, variant, last, n in session.execute(query).all():
            out[(train_id, variant)] = (last, n)
    return out


def _variant_per_train_at(
    session: Session, station_id: int, fv_id: int, train_ids=None
) -> dict[int, str | None]:
    """The one route variant (StationStop.path_sha1) to show each train by at
    a station.

    A train holds one station_stop row per variant it has run as, so the same
    call at a station is often stored two or three times, sometimes a minute
    apart. Showing every row lists one trip several times. The variant chosen
    is the one the train ran as most recently (then most often) among those
    that call here at all, so a short variant that skips this station never
    hides the train.
    """
    stop_query = select(StationStop.train_id, StationStop.path_sha1).where(
        StationStop.station_id == station_id,
        StationStop.feed_version_id == fv_id,
    )
    if train_ids is not None:
        stop_query = stop_query.where(StationStop.train_id.in_(list(train_ids)))
    present: dict[int, set] = {}
    for train_id, variant in session.execute(stop_query.distinct()).all():
        present.setdefault(train_id, set()).add(variant)

    contested = [t for t, variants in present.items() if len(variants) > 1]
    rank = _variant_runs(session, contested)

    chosen: dict[int, str | None] = {}
    for train_id, variants in present.items():
        chosen[train_id] = max(
            variants,
            key=lambda v: (rank.get((train_id, v), ("", 0)), v or ""),
        )
    return chosen


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

    # Each (train, variant) is one sequence of stops. A numbered TT train can hold more than one
    # variant (a wing/coupled service some days, solo others -- see StationStop.path_sha1),
    # sharing stop_sequence numbers for different physical stops, so a sequence is never built
    # by interleaving two variants.
    sequences: dict[tuple[int, str | None], list[tuple[int, int]]] = {}
    names: dict[int, str] = {}
    for train_id, variant, station_id, seq, name in rows:
        sequences.setdefault((train_id, variant), []).append((seq, station_id))
        names.setdefault(station_id, name)
    # Identical sequences add nothing; the longest goes first and sets the main order.
    distinct = {tuple(sid for _seq, sid in sorted(stops)) for stops in sequences.values()}
    ordered_ids = _merge_stop_orders(sorted(distinct, key=lambda stops: (-len(stops), stops)))
    return [{"id": station_id, "name": names[station_id]} for station_id in ordered_ids]


def _merge_stop_orders(sequences: list[tuple[int, ...]]) -> list[int]:
    """One travel order for every stop of several runs, the first run's order kept as it is.

    A stop the order does not have yet goes between the stops its own run called at around it:
    just before the stop where the run rejoins the order (a detour, or a run coming in from a
    branch), or else right after the stop where it left (a branch to its own end). Branch stops
    so sit by their junction instead of piling up at the end of the list.

    Args:
        sequences: each run's stations in its own calling order, the run to follow first.

    Returns:
        Every station once, in travel order.
    """
    order: list[int] = []
    placed: set[int] = set()
    for stops in sequences:
        for i, station_id in enumerate(stops):
            if station_id in placed:
                continue
            placed.add(station_id)
            before = next((sid for sid in reversed(stops[:i]) if sid in placed and sid != station_id), None)
            after = next((sid for sid in stops[i + 1:] if sid in placed), None)
            if after is not None and (before is None or order.index(after) > order.index(before)):
                # A detour (or a run joining from a branch): its stops go just before it rejoins.
                order.insert(order.index(after), station_id)
            elif before is not None:
                order.insert(order.index(before) + 1, station_id)
            else:
                order.append(station_id)
    return order



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
    n_before: int = 2,
    n_after: int = 1,
    offset: int | None = None,
    count: int | None = None,
) -> dict | None:
    """The `n_before` scheduled calls at or before a time and the `n_after` right after (2 and 1).

    The schedule is a recurring daily pattern -- that is the point of asking for
    a time of day rather than a timestamp -- so "before" and "after" wrap around
    midnight rather than running out at either end of the day. `day_type` picks
    which pool the model conditions its forecast on for each trip; it does not
    affect which trips are considered, since the underlying timetable pattern is
    itself dateless. Callers with an actual calendar date use resolve_day_type
    first, and pass that same date on as `service_date` for the model (which,
    unlike the averages, needs an actual date rather than a day-type bucket). A page that
    slides its window earlier or later asks for more calls on that side.

    `offset` and `count` ask for calls by position instead: position 0 is the first call after
    the chosen time, -1 the last at or before it, and so on around the day. `count` calls from
    `offset` come back, each carrying its `position`; they win over `n_before`/`n_after`.
    """
    line = _line_header(session, line_id)
    station = session.get(Station, station_id)
    if line is None or station is None or direction not in (0, 1):
        return None

    _, halves = _line_trains_by_direction(session, line_id)
    header = {"id": line["id"], "name": line["name"]}
    return _trips_around_in(
        session, header, halves[direction], direction, station, time_of_day_s,
        day_type=day_type, line_id=line_id, service_date=service_date,
        n_before=n_before, n_after=n_after, offset=offset, count=count,
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
    n_before: int = 2,
    n_after: int = 1,
    offset: int | None = None,
    count: int | None = None,
) -> dict | None:
    """trips_around for a line-less train's route; averages come per train."""
    station = session.get(Station, station_id)
    if station is None or direction not in (0, 1):
        return None
    grouped = _corridor_trains_by_direction(session, train_id)
    if grouped is None:
        return None
    anchor = session.get(Train, train_id)
    header = {"id": anchor.id, "name": anchor.display_name or f"#{anchor.id}"}
    return _trips_around_in(
        session, header, grouped[1][direction], direction, station, time_of_day_s,
        day_type=day_type, line_id=None, service_date=service_date,
        n_before=n_before, n_after=n_after, offset=offset, count=count,
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
    n_before: int = 2,
    n_after: int = 1,
    offset: int | None = None,
    count: int | None = None,
) -> dict:
    """The body of trips_around, for any set of trains running one direction.

    Besides the picked calls, the payload's `n_calls` is how many distinct calls the stop has,
    so a page knows how far its window can slide before it would repeat a run.

    `line_id` None means line-less trains: their averages are looked up per
    train (train_stop_delay_average) rather than per line and direction.
    """
    station_id = station.id
    trains_by_id = {t.id: t for t in trains}
    fv_id = tt_feed_version_id(session)
    model = current_model()
    model_service_date = service_date or local_date(int(time.time()))
    holiday_set = load_holiday_dates(session) if model is not None else None
    line_directions_cache: dict[int, dict[int, int]] = {}
    model_direction = _ModelDirectionCache(line_directions_cache)

    payload = {
        "line": header,
        "direction": direction,
        "station": {"id": station.id, "name": station.name},
        "model_stamp": model[0] if model is not None else None,
        "n_calls": 0,
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
    else:
        # One variant per train, or one trip is listed once per variant.
        variant_of = _variant_per_train_at(session, station_id, fv_id, trains_by_id)
        rows = [(stop,) for (stop,) in rows if stop.path_sha1 == variant_of.get(stop.train_id)]

    candidates = []
    for (stop,) in rows:
        sched = stop.planned_departure_sec
        if sched is None:
            sched = stop.planned_arrival_sec
        candidates.append((sched % 86400, stop))
    # id breaks ties: two runs at the same minute must come out in the same order on every copy
    # of the data, whatever order the query returned them in.
    candidates.sort(key=lambda c: (c[0], c[1].id))

    n = len(candidates)
    if n == 0:
        return payload
    payload["n_calls"] = n

    times = [c[0] for c in candidates]
    after_idx = bisect.bisect_right(times, time_of_day_s)

    # The `n_before` before/at and the `n_after` after, wrapping through index 0 when the
    # chosen time is near either edge of the day. With fewer than 3 runs total
    # the same stop is picked more than once; dedup on the way out rather than
    # pretending three distinct trips exist when they do not.
    # Position in this 3-slot picture, not a fresh tod comparison: wrapping past
    # midnight can put the "next" call earlier in the day than the chosen time
    # (23:00 -> next is 06:00), and it must still read as "after", not "before".
    first, last = (-n_before, n_after) if offset is None else (offset, offset + (count or 0))
    picks = [
        (after_idx + position, position, "before" if position < 0 else "after")
        for position in range(first, last)
    ]

    seen: set[int] = set()
    picked: list[tuple] = []
    for i, position, relation in picks:
        _tod, stop = candidates[i % n]
        # By position, the schedule repeats day after day, so a short day shows the same run again
        # on the next day: each position is returned. Without positions, a run is listed once.
        if offset is None:
            if stop.id in seen:
                continue
            seen.add(stop.id)
        # Which day this run is on, counted from the chosen date: 0 for the rest of that day, 1 for
        # the next, -1 for the day before.
        picked.append((stop, relation, position, i // n))

    is_holiday_flag = _is_holiday_flag(day_type)
    averages_direction = _AveragesDirection(line_directions_cache)
    trips = []
    for stop, relation, position, day_offset in picked:
        train = trains_by_id[stop.train_id]
        trip_start_time = _train_trip_start_time(train)
        # The averages are keyed by service_line and its own direction split,
        # not by the (passenger) line this page shows.
        pred_kwargs = dict(
            station_id=station_id,
            line_id=None if line_id is None else train.line_id,
            direction=(
                direction if line_id is None else averages_direction.get(session, train)
            ),
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
                service_date=model_service_date + timedelta(days=day_offset),
                direction=model_direction.get(session, train),
                holiday_set=holiday_set,
                arr_sec=stop.planned_arrival_sec,
                dep_sec=stop.planned_departure_sec,
                variant=stop.path_sha1,
            )

        trips.append(
            {
                "train_id": train.id,
                "name": passenger_name(session, train),
                "number_name": train.display_name or f"#{train.id}",
                "url": f"/train/{train.id}/",
                "sched_arr": _hhmm(stop.planned_arrival_sec),
                "sched_dep": _hhmm(stop.planned_departure_sec),
                "arr_pred": _delay_pred(session, side="arr", **pred_kwargs),
                "dep_pred": _delay_pred(session, side="dep", **pred_kwargs),
                "model_input": model_input,
                "relation": relation,
                "position": position,
                "day_offset": day_offset,
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
    line = _line_header(session, line_id)
    if line is None:
        return None

    # Dateless, like train_route: a per-run figure has no specific service date
    # to ask the model about, so it is asked about today.
    model = current_model()
    model_service_date = local_date(int(time.time()))
    holiday_set = load_holiday_dates(session) if model is not None else None
    line_directions_cache: dict[int, dict[int, int]] = {}
    model_direction = _ModelDirectionCache(line_directions_cache)

    canonical, halves = _line_trains_by_direction(session, line_id, limit=limit)
    line_train_ids = [t.id for trains in halves for t in trains]

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
                .where(
                    StationStop.train_id.in_(line_train_ids),
                    StationStop.feed_version_id == fv_id,
                )
                .group_by(StationStop.train_id)
            ).all()
        )
        if fv_id is not None and line_train_ids
        else {}
    )

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

    averages_direction = _AveragesDirection(line_directions_cache)
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
                        # Averages are keyed by service_line, not the passenger line.
                        line_id=t.line_id,
                        direction=averages_direction.get(session, t),
                        trip_start_time=trip_start_time,
                        sched_time=trip_start_time,
                        is_holiday=is_holiday_flag,
                        side="dep",
                    ),
                    "model_input": model_input,
                }
            )

    return {
        "line": line,
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
