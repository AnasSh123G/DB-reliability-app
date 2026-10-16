"""Check that the server answers from the deployed database exactly as from the collection database.

The web layer's own query functions are called on both databases with the same arguments, and the
results compared as JSON: search, station boards, train routes (with and without the stop a page
was opened from), line and corridor directions, stations and details, and the Prognose flow's
earlier and later runs around a time. `scripts/compare_deployed_db.py` runs it on real data.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dbrel.models import PassengerLine, ServiceLine, Station, StationStop, Train
from dbrel.config import TZ
from dbrel.web import queries

TIMES_OF_DAY = (7 * 3600, 12 * 3600, 17 * 3600 + 1800)


@dataclass(frozen=True)
class Request:
    label: str
    call: Callable[[Session], object]


def json_diff(a, b, path: str = "") -> list[str]:
    """Every JSON path at which `a` and `b` differ, with both values."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b), key=str):
            if key not in a or key not in b:
                out.append(f"{path}/{key}: only in the {'collection' if key in a else 'deployed'} database")
            else:
                out += json_diff(a[key], b[key], f"{path}/{key}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        out = [f"{path}: {len(a)} items vs {len(b)}"] if len(a) != len(b) else []
        for i, (x, y) in enumerate(zip(a, b)):
            out += json_diff(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [f"{path or '/'}: {str(a)[:60]!r} vs {str(b)[:60]!r}"]


def _public_line_ids(session: Session) -> list[int]:
    """The line ids the web uses on `session`: passenger lines when built, else service lines."""
    ids = session.execute(select(PassengerLine.id)).scalars().all()
    return list(ids) or list(session.execute(select(ServiceLine.id)).scalars().all())


def _ends_and_middle(stations: list[dict]) -> list[int]:
    """The ids of the first, middle and last of `stations` (dicts with an `id`), sorted."""
    if not stations:
        return []
    picks = {stations[0]["id"], stations[len(stations) // 2]["id"], stations[-1]["id"]}
    return sorted(picks)


def requests_for(session: Session, *, now: int, service_date: date, stations=(), trains=(), lines=(),
                 corridors=(), terms=(), plans=()) -> list[Request]:
    """The requests for the given stations, trains, line ids and line-less trains.

    Directions and stops for the runs-around requests are looked up on `session` (any of the two
    databases: they agree on these, and the comparison would report it if they did not).
    """
    out: list[Request] = []
    day = service_date.isoformat()

    def around(kind: str, fetch, key: int, stations_of, direction: int) -> None:
        """Add runs-around requests for line or corridor `key` in `direction`.

        `kind` labels the requests, `fetch` is the web function to call and `stations_of` lists the
        route's stations; one request per picked stop and time of day.
        """
        on_route = stations_of(session, key, direction) or []
        # The ends and the middle, plus any requested station the route calls at (e.g. a stop a
        # user reported).
        picked = set(_ends_and_middle(on_route)) | ({s["id"] for s in on_route} & set(stations))
        for station_id in sorted(picked):
            station = session.get(Station, station_id)
            day_type = queries.resolve_day_type(session, day, station=station)
            for t in TIMES_OF_DAY:
                out.append(Request(
                    f"{kind} {key} around dir {direction} station {station_id} {t // 3600:02d}:{t % 3600 // 60:02d}",
                    lambda s, k=key, d=direction, st=station_id, tt=t, dt=day_type: fetch(
                        s, k, d, st, tt, day_type=dt, service_date=service_date)))

    for term in terms:
        out.append(Request(f"suggest {term!r}", lambda s, q=term: queries.suggest(s, q)))
    for station_id in stations:
        out.append(Request(f"board {station_id}", lambda s, i=station_id: queries.station_board(s, i, now=now)))
    for train_id in trains:
        out.append(Request(f"route {train_id}", lambda s, i=train_id: queries.train_route(s, i)))
        called = session.execute(select(StationStop.station_id).where(StationStop.train_id == train_id)
                                 .distinct().order_by(StationStop.station_id)).scalars().all()
        for station_id in called[:6]:
            out.append(Request(f"route {train_id} from {station_id}",
                               lambda s, i=train_id, st=station_id: queries.train_route(s, i, station_id=st)))
    for line_id in lines:
        out.append(Request(f"line {line_id} directions", lambda s, i=line_id: queries.line_directions(s, i)))
        out.append(Request(f"line {line_id} detail", lambda s, i=line_id: queries.line_detail(s, i)))
        for direction in (0, 1):
            out.append(Request(f"line {line_id} stations dir {direction}",
                               lambda s, i=line_id, d=direction: queries.line_stations(s, i, d)))
            around("line", queries.trips_around, line_id, queries.line_stations, direction)
    for train_id in corridors:
        out.append(Request(f"corridor {train_id} directions", lambda s, i=train_id: queries.corridor_directions(s, i)))
        for direction in (0, 1):
            out.append(Request(f"corridor {train_id} stations dir {direction}",
                               lambda s, i=train_id, d=direction: queries.corridor_stations(s, i, d)))
            around("corridor", queries.corridor_trips_around, train_id, queries.corridor_stations, direction)
    monday = service_date - timedelta(days=service_date.weekday())
    at = int(datetime(monday.year, monday.month, monday.day, 8, tzinfo=TZ).timestamp())
    picked = list(dict.fromkeys(stations))[:3]
    pairs = list(plans)
    if len(picked) == 3:
        pairs = list(zip(picked, picked[1:] + picked[:1])) + pairs
    directions = dict.fromkeys((a, b) for origin, target in pairs
                               for a, b in ((origin, target), (target, origin)) if a != b)

    def plan_payload(s, origin, target):
        body = queries.plan(s, origin, target, at, monday.isoformat(), "08:00")[1]
        return {key: value for key, value in body.items() if key != "stats"}

    for origin, target in directions:
        out.append(Request(f"plan {origin}>{target}",
                           lambda s, a=origin, b=target: plan_payload(s, a, b)))
    return out


def sample(session: Session, *, n_stations: int, n_trains: int, n_lines: int, n_corridors: int,
           seed: int = 0) -> dict[str, list]:
    """A reproducible sample: the busiest stations and the trains with the most variants, plus random ones."""
    rng = random.Random(seed)
    tt = queries.tt_feed_version_id(session)
    busiest = session.execute(
        select(StationStop.station_id).where(StationStop.feed_version_id == tt)
        .group_by(StationStop.station_id).order_by(func.count().desc()).limit(3)).scalars().all()
    tracked = session.execute(select(Station.id).where(Station.tracked == 1).order_by(Station.id)).scalars().all()
    stations = list(busiest) + rng.sample([s for s in tracked if s not in busiest],
                                          min(n_stations, max(0, len(tracked) - len(busiest))))
    most_variants = session.execute(
        select(StationStop.train_id).where(StationStop.feed_version_id == tt).group_by(StationStop.train_id)
        .order_by(func.count(func.distinct(StationStop.path_sha1)).desc(), StationStop.train_id).limit(5)
    ).scalars().all()
    numbered = session.execute(select(Train.id).where(Train.line_id.is_not(None)).order_by(Train.id)).scalars().all()
    trains = list(most_variants) + rng.sample(numbered, min(n_trains, len(numbered)))
    line_ids = _public_line_ids(session)
    lines = rng.sample(line_ids, min(n_lines, len(line_ids)))
    lineless = session.execute(select(Train.id).where(Train.line_id.is_(None), Train.route_path.is_not(None))
                               .order_by(Train.id)).scalars().all()
    corridors = rng.sample(lineless, min(n_corridors, len(lineless)))
    terms = [session.get(Station, s).name for s in stations[:5]] + ["RE4", "ICE", "S1"]
    return {"stations": stations, "trains": trains, "lines": lines, "corridors": corridors, "terms": terms}


def _as_json(value):
    """`value` as the JSON the web would send (dates and other objects as strings)."""
    return json.loads(json.dumps(value, default=str))


def compare(collection: Engine, deployed: Engine, requests: list[Request], *,
            log: Callable[[str], None] = print) -> list[str]:
    """Run every request on both databases; returns 'label: path: a vs b' for each difference."""
    differences: list[str] = []
    started = time.monotonic()
    with Session(collection) as a, Session(deployed) as b:
        for i, request in enumerate(requests, start=1):
            mine, theirs = _as_json(request.call(a)), _as_json(request.call(b))
            differences += [f"{request.label}: {d}" for d in json_diff(mine, theirs)]
            if i % 100 == 0:
                log(f"  {i}/{len(requests)} requests, {len(differences)} differences, "
                    f"{time.monotonic() - started:.0f}s")
            a.rollback()
            b.rollback()
    return differences
