"""Live accuracy check: score TabM and the stored averages against real, freshly-fetched
outcomes from the DB Timetables API.

One `/fchg` call per sampled station is the whole data source (see
openspec/changes/add-live-accuracy-study/design.md): it returns every train currently passing
through that station, each carrying both its scheduled time and, once it has actually happened,
its real recorded time. From that single response, this keeps only stops with a genuine
published outcome on a side -- not merely a live estimate -- and, for each, computes:

- TabM's prediction, through `dbrel.ml.predict.DelayModel`, the same class `predict-delay` and
  the browser endpoint use
- the stored average exactly as the live station board would show it, through
  `dbrel.web.queries._delay_pred`

and scores both against the real recorded delay with the existing metrics
(`dbrel.ml.metrics`).

Read-only throughout: nothing here writes to `trip_stop`, the averages tables, or any model
artifact.
"""

from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.config import TZ
from dbrel.direction import direction_by_train_id
from dbrel.identity import SOURCE_TT, train_identity_key
from dbrel.ingest.changes_poller import GRACE_S
from dbrel.ingest.client import TimetablesClient
from dbrel.ingest.parser import TtEvent, TtStop, parse_timetable
from dbrel.ingest.timeutil import epoch_to_gtfs_sec, local_date, read_clock
from dbrel.ml.predict import DelayModel
from dbrel.ml.timetable import ScheduledCall, calls_at
from dbrel.models import DAY_TYPE_SUNDAY_HOLIDAY, Station, Train
from dbrel.web.queries import _delay_pred, _ModelDirectionCache, _sched_time_key, _train_trip_start_time, resolve_day_type

SIDES = ("dep", "arr")  # dep/arr, not arr/dep, to match TabM's own output order (nn.py)


# --------------------------------------------------------------------------------------
# 1. Sample stations and fetch live data
# --------------------------------------------------------------------------------------


def sample_stations(session: Session, n: int, *, seed: int | None = None) -> list[Station]:
    """`n` distinct stations, drawn at random without replacement, from every station with an
    EVA number (the DB Timetables API is keyed on EVA, so a station without one can never be
    queried)."""
    ids = session.execute(select(Station.id).where(Station.eva.isnot(None))).scalars().all()
    rng = random.Random(seed)
    chosen = rng.sample(ids, min(n, len(ids)))
    stations = {s.id: s for s in session.execute(select(Station).where(Station.id.in_(chosen))).scalars()}
    return [stations[i] for i in chosen]


@dataclass(slots=True)
class RealizedSide:
    station: Station
    stop: TtStop
    side: str  # "arr" or "dep"
    event: TtEvent
    real_delay_min: float


def realized_sides(station: Station, stops: list[TtStop], now: int) -> list[RealizedSide]:
    """Every (stop, side) with a real, published outcome -- not merely a live estimate.

    `ct` is IRIS's live estimate for a train that hasn't arrived yet, not only a record of one
    that has, so its mere presence is not the test. A side counts once its changed time is more
    than GRACE_S in the past (the same rule `changes_poller._has_happened` draws this line
    with) -- and only a changed time gives a real delay to score at all; the planned-time
    fallback that function also allows exists for a different purpose there and is not reused
    here.
    """
    out = []
    for stop in stops:
        for side, event in (("arr", stop.arrival), ("dep", stop.departure)):
            if event is None or event.is_cancelled:
                continue
            if event.changed_ts is None or event.planned_ts is None:
                continue
            if event.changed_ts + GRACE_S >= now:
                continue
            out.append(RealizedSide(station, stop, side, event, event.delay_s / 60))
    return out


@dataclass(slots=True)
class FetchResult:
    realized: list[RealizedSide] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None


LOOKBACK_HOURS = 2


def _get(fetch) -> tuple[list[TtStop] | None, str | None]:
    """Run one request and parse it; a failure is returned as a reason, never raised."""
    try:
        resp = fetch()
    except Exception as exc:  # noqa: BLE001 -- any transport failure is a skip, not fatal
        return None, f"request failed: {exc}"
    if not resp.ok:
        return None, f"http {resp.status}"
    try:
        return parse_timetable(resp.body).stops, None
    except Exception as exc:  # noqa: BLE001 -- a malformed response is a skip, not fatal
        return None, f"unparseable response: {exc}"


def merge_plan_and_changes(plan: list[TtStop], changes: list[TtStop]) -> list[TtStop]:
    """Planned stops with their changed times filled in from /fchg, matched by stop id.

    /fchg is a pure delta for an ordinary run: it carries `ct`/`cs` but no train number and
    no planned time (see changes_poller._is_full_record), so on its own it cannot say which
    train a stop is or how late it was. The train and planned time come from /plan; only the
    changed time and status come from /fchg -- the same split `dbrel collect` relies on.
    A planned stop with no /fchg entry is left as planned only, so it is never scored.
    """
    by_id = {s.stop_id: s for s in changes}
    out, seen = [], set()
    for stop in plan:
        change = by_id.get(stop.stop_id)
        if change is None or stop.stop_id in seen:
            continue
        seen.add(stop.stop_id)
        for attr in ("arrival", "departure"):
            planned, changed = getattr(stop, attr), getattr(change, attr)
            if planned is None or changed is None:
                continue
            setattr(stop, attr, dataclasses.replace(
                planned, changed_ts=changed.changed_ts, changed_status=changed.changed_status))
        out.append(stop)
    return out


def fetch_station(client: TimetablesClient, station: Station, now: int, *,
                  lookback_hours: int = LOOKBACK_HOURS) -> FetchResult:
    """The station's trains of the last `lookback_hours`, reduced to their already-realized
    sides: one /plan call per hour (train + planned time) and one /fchg call (real time).

    A failed or empty fetch is never fatal to the run -- it is reported back as a skip, the
    same way a station timing out during a sweep is data about coverage, not an error.
    """
    changes, reason = _get(lambda: client.changes(station.eva))
    if changes is None:
        return FetchResult(skipped=True, skip_reason=reason)
    plan: list[TtStop] = []
    for back in range(lookback_hours, -1, -1):
        moment = datetime.fromtimestamp(now - back * 3600, TZ)
        stops, _ = _get(lambda m=moment: client.plan(station.eva, m.strftime("%y%m%d"), m.hour))
        plan.extend(stops or [])  # an hour /plan no longer serves is just an empty hour
    realized = realized_sides(station, merge_plan_and_changes(plan, changes), now)
    if not realized:
        return FetchResult(skipped=True, skip_reason="no realized stops")
    return FetchResult(realized=realized)


# --------------------------------------------------------------------------------------
# 2. Resolve each realized side to a scheduled call
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ScoredStop:
    side: RealizedSide
    call: ScheduledCall
    train: Train


def resolve_call(session: Session, side: RealizedSide) -> ScoredStop | None:
    """The realized side's train and scheduled call from the local timetable, or `None`.

    Route shape (`stop_rank`/`n_stops`) is *not* fetched live -- a bare `/fchg` stop doesn't
    carry a trip's other stops -- it comes from `StationStop`, the static timetable pattern
    (`dbrel.ml.timetable.calls_at`), the same lookup `predict.py`'s live path already uses.
    """
    stop = side.stop
    identity = train_identity_key(SOURCE_TT, stop.category, stop.number)
    train = session.execute(select(Train).where(Train.identity_key == identity)).scalar_one_or_none()
    if train is None:
        return None
    service_date = date_cls.fromisoformat(stop.service_date)
    calls = calls_at(session, train, side.station.id, service_date)
    if not calls:
        return None
    call = _pick_call(calls, side.event, service_date)
    return ScoredStop(side=side, call=call, train=train)


def _pick_call(calls: list[ScheduledCall], event: TtEvent, service_date: date_cls) -> ScheduledCall:
    """The call among a looped route's several visits that this event belongs to.

    Matched the same way `web.queries._model_input` disambiguates a loop: by comparing planned
    times, converted to the same service-day-second form `StationStop` stores them in. Falls
    back to the route's main variant (`calls[0]`) when nothing matches -- an ordinary,
    non-looped stop, the overwhelming majority.
    """
    if len(calls) == 1 or event.planned_ts is None:
        return calls[0]
    planned_sec = epoch_to_gtfs_sec(service_date, event.planned_ts)
    for candidate in calls:
        if planned_sec in (candidate.planned_arrival_sec, candidate.planned_departure_sec):
            return candidate
    return calls[0]


# --------------------------------------------------------------------------------------
# 3. Get both predictions
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Comparison:
    side: str  # "arr" or "dep"
    segment: str | None
    is_holiday: int
    service_date: str
    real_delay_min: float
    tabm: tuple[float, float, float] | None  # p25, p50, p75
    average: tuple[float, float, float] | None  # lo=hi=p50, per _delay_pred
    station: str | None = None
    train: str | None = None


def score_stops(session: Session, model: DelayModel, stops: list[ScoredStop], *, now: int,
                progress=lambda msg: None) -> list[Comparison]:
    """TabM's and the stored average's prediction for each stop, next to its real delay.

    TabM runs once, batched, through `DelayModel.predict_calls` -- the exact method
    `predict-delay` and the browser endpoint use, so its number is never a reimplementation.
    The average is `_delay_pred` itself, with the request-time day type `station_board` would
    use right now (not each call's own service date -- an existing property of the live board,
    reproduced here on purpose so the comparison is against what a viewer would actually see).
    """
    direction_cache: dict[int, dict[int, int]] = {}  # line_id -> {train_id: direction}
    model_direction = _ModelDirectionCache(direction_cache)
    calls = [s.call for s in stops]
    progress(f"running TabM on {len(calls):,} stops")
    predictions = model.predict_calls(session, calls, direction=model_direction.get) if calls else []
    progress("looking up the stored averages")
    today = local_date(now).isoformat()
    holiday_by_station: dict[int, int] = {}
    out = []
    for i, (scored, prediction) in enumerate(zip(stops, predictions), 1):
        if i % 10_000 == 0:
            progress(f"  {i:,} / {len(stops):,}")
        side, train = scored.side, scored.train
        station = side.station
        if station.id not in holiday_by_station:
            holiday_by_station[station.id] = 1 if resolve_day_type(
                session, today, station=station) == DAY_TYPE_SUNDAY_HOLIDAY else 0
        is_holiday = holiday_by_station[station.id]
        direction = None
        if train.line_id is not None:
            dirs = direction_cache.get(train.line_id)
            if dirs is None:
                dirs = direction_by_train_id(session, train.line_id)
                direction_cache[train.line_id] = dirs
            direction = dirs.get(train.id)
        avg = _delay_pred(
            session,
            station_id=station.id,
            line_id=train.line_id,
            direction=direction,
            trip_start_time=_train_trip_start_time(train),
            sched_time=_sched_time_key(scored.call.planned_arrival_sec, scored.call.planned_departure_sec),
            is_holiday=is_holiday,
            side=side.side,
            train_id=train.id,
        )
        tabm = prediction.arr if side.side == "arr" else prediction.dep
        out.append(Comparison(
            side=side.side,
            segment=train.segment,
            is_holiday=is_holiday,
            service_date=side.stop.service_date,
            real_delay_min=side.real_delay_min,
            tabm=tabm,
            average=(avg["lo_min"], avg["p50_min"], avg["hi_min"]) if avg else None,
            station=station.name,
            train=train.display_name,
        ))
    return out


# --------------------------------------------------------------------------------------
# 4. Score and report
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class StudyResult:
    stations_sampled: int
    stations_skipped: int
    skip_reasons: dict[str, int]
    stops_skipped_no_local_match: int
    scores: dict  # side -> {"tabm": {...}, "average": {...}}
    n_scored: dict[str, int]
    comparisons: list[Comparison] = field(default_factory=list)  # every stop, for comparisons.csv


WITHIN_MIN = (1, 3, 5)


def _errors(errors: np.ndarray) -> dict:
    """How far off a predictor was, in plain terms: the average miss and how often it was close."""
    if len(errors) == 0:
        return {"minutes_off": None, **{f"within_{m}": None for m in WITHIN_MIN}}
    return {"minutes_off": float(np.mean(errors)),
            **{f"within_{m}": float(np.mean(errors <= m)) for m in WITHIN_MIN}}


def score_comparisons(comparisons: list[Comparison]) -> dict:
    """Per side: TabM's median guess and the stored average, each against the real delay.

    Both are compared on the same stops -- the ones that have a stored average -- so the two
    columns are like for like. Stops with no stored average are counted, and TabM's own miss on
    them is given separately.
    """
    out = {}
    for side in SIDES:
        rows = [c for c in comparisons if c.side == side and c.tabm is not None]
        both = [r for r in rows if r.average is not None]
        only_tabm = [r for r in rows if r.average is None]
        real = np.array([r.real_delay_min for r in both])
        tabm_err = np.abs(np.array([r.tabm[1] for r in both]) - real)
        avg_err = np.abs(np.array([r.average[1] for r in both]) - real)
        out[side] = {
            "n": len(rows),
            "n_compared": len(both),
            "tabm": _errors(tabm_err),
            "average": _errors(avg_err),
            "tabm_closer": int(np.sum(tabm_err < avg_err)),
            "average_closer": int(np.sum(avg_err < tabm_err)),
            "tie": int(np.sum(tabm_err == avg_err)),
            "n_no_average": len(only_tabm),
            "tabm_on_no_average": _errors(np.abs(np.array(
                [r.tabm[1] - r.real_delay_min for r in only_tabm]))),
        }
    return out


def run_study(
    session: Session,
    client: TimetablesClient,
    models_dir: Path,
    *,
    sample_size: int = 3000,
    seed: int | None = None,
    now: int | None = None,
    progress=lambda msg: None,
) -> StudyResult:
    now = read_clock(now)
    model = DelayModel.current(models_dir)  # raises NoModelError if none is trained

    stations = sample_stations(session, sample_size, seed=seed)
    progress(f"sampled {len(stations)} stations")

    realized: list[RealizedSide] = []
    stations_skipped = 0
    skip_reasons: dict[str, int] = {}
    for station in stations:
        result = fetch_station(client, station, now)
        if result.skipped:
            stations_skipped += 1
            skip_reasons[result.skip_reason] = skip_reasons.get(result.skip_reason, 0) + 1
            continue
        realized.extend(result.realized)
    progress(f"{len(stations) - stations_skipped} stations had realized stops; "
             f"{len(realized)} realized sides fetched")

    resolved: list[ScoredStop] = []
    stops_skipped_no_local_match = 0
    for side in realized:
        scored = resolve_call(session, side)
        if scored is None:
            stops_skipped_no_local_match += 1
            continue
        resolved.append(scored)
    progress(f"{len(resolved)} sides resolved to a local timetable match; "
             f"{stops_skipped_no_local_match} had none")

    comparisons = score_stops(session, model, resolved, now=now, progress=progress)
    scores = score_comparisons(comparisons)
    n_scored = {side: scores[side]["n"] for side in SIDES}

    return StudyResult(
        stations_sampled=len(stations),
        stations_skipped=stations_skipped,
        skip_reasons=skip_reasons,
        stops_skipped_no_local_match=stops_skipped_no_local_match,
        scores=scores,
        n_scored=n_scored,
        comparisons=comparisons,
    )


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------

RESULT_NAME = "result.json"
COMPARISONS_NAME = "comparisons.csv"
REPORT_NAME = "report.md"


def _fmt(value, digits: int = 2) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _pct(value) -> str:
    return "–" if value is None else f"{100 * value:.1f}%"


def render_report(result: StudyResult, *, created_at: str) -> str:
    """Plain numbers: how many minutes each predictor was off, how often it was close, and
    which one was closer, on the same stops."""
    lines = [
        f"# Live accuracy check — {created_at}",
        "",
        f"- Stations sampled: {result.stations_sampled}, of which {result.stations_skipped} "
        "had no train that had already arrived/departed in the look-back window"
        + (f" ({', '.join(f'{n} {reason}' for reason, n in sorted(result.skip_reasons.items()))})"
           if result.skip_reasons else ""),
        f"- Stops whose train isn't in the local timetable (skipped): "
        f"{result.stops_skipped_no_local_match}",
        "",
        "Real delay = what DB recorded for that stop. TabM's figure is its median guess; the "
        "stored average is the Ø the app shows.",
        "",
    ]
    for side, title in (("dep", "Departures"), ("arr", "Arrivals")):
        s = result.scores[side]
        lines += [f"## {title}", ""]
        if s["n_compared"] == 0:
            lines += ["Nothing to compare.", ""]
            continue
        lines += [
            f"Compared on {s['n_compared']:,} stops that have both a TabM guess and a stored average.",
            "",
            "| | Average minutes off | Within 1 min | Within 3 min | Within 5 min |",
            "|---|---|---|---|---|",
        ]
        for name, key in (("TabM", "tabm"), ("Stored average", "average")):
            e = s[key]
            lines.append(f"| {name} | {_fmt(e['minutes_off'], 1)} | {_pct(e['within_1'])} | "
                         f"{_pct(e['within_3'])} | {_pct(e['within_5'])} |")
        lines += [
            "",
            f"Closer to the real delay: TabM {s['tabm_closer']:,} times, stored average "
            f"{s['average_closer']:,} times, same {s['tie']:,} times.",
        ]
        if s["n_no_average"]:
            e = s["tabm_on_no_average"]
            lines.append(f"{s['n_no_average']:,} more stops had no stored average; TabM was "
                         f"{_fmt(e['minutes_off'], 1)} min off on those.")
        lines.append("")
    return "\n".join(lines)


def save_report(result: StudyResult, studies_dir: Path, *, stamp: str) -> Path:
    """Writes `result.json`, `report.md` and `comparisons.csv` (one row per stop) under
    `studies_dir/stamp/`. Never overwrites an earlier study: a repeat run gets its own stamp."""
    import csv
    import json

    out_dir = studies_dir / stamp
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} already exists")
    out_dir.mkdir(parents=True)
    payload = {f.name: getattr(result, f.name) for f in dataclasses.fields(result)
               if f.name != "comparisons"}
    (out_dir / RESULT_NAME).write_text(json.dumps(payload, indent=1))
    with (out_dir / COMPARISONS_NAME).open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["side", "station", "train", "service_date", "segment", "is_holiday",
                    "real_delay_min", "tabm_p25", "tabm_p50", "tabm_p75", "average_min"])
        for c in result.comparisons:
            tabm = c.tabm or (None, None, None)
            w.writerow([c.side, c.station, c.train, c.service_date, c.segment, c.is_holiday,
                        c.real_delay_min, *(None if v is None else round(v, 3) for v in tabm),
                        None if c.average is None else c.average[1]])
    (out_dir / REPORT_NAME).write_text(render_report(result, created_at=stamp))
    if result.comparisons:
        from dbrel.study.histogram import write_histogram

        write_histogram(out_dir)
    return out_dir
