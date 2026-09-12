"""Resolving a gtfs.de station to an IRIS eva, and proving the mapping is right.

This is the highest silent-failure risk in the whole ingest. A wrong eva
attributes another place's trains to a station and is invisible in aggregate —
no downstream check can detect it. The probe hit exactly that on its first
attempt: querying "Wanne-Eickel" returns a *bus stop* at eva 212342, while the
railway station is "Herne-Wanne-Eickel Hbf" at 8000192 and only the DS100 code
finds it (docs/probe-findings.md §14).

Three gates, all of which must pass before a station may be polled:

1. the eva must be a railway eva (8000000-8099999);
2. the IRIS name must loosely match the gtfs.de name;
3. where GTFS pattern data is loaded, the ppth neighbours must overlap the
   neighbours GTFS records for that station.

`/station/` returns no coordinates, so it cannot self-verify; gate 3 is the only
behavioural check available without a second data source.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.config import RAIL_EVA_RANGE
from dbrel.identity import normalize_name
from dbrel.models import Station, StationStop
from dbrel.timetables.client import TimetablesClient
from dbrel.timetables.parser import parse_stations, parse_timetable

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# gtfs.de appends product markers IRIS does not use, and vice versa.
_SUFFIX = re.compile(r"(hauptbahnhof|bahnhof|hbf|bhf|bf|s)$")
_PAREN_CITY = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")
_TRAILING_BF = re.compile(r"\s+(Bhf|Bf)$")


def loose_name(name: str) -> str:
    """Punctuation- and suffix-insensitive key.

    'Herne-Wanne-Eickel Hbf' == 'Herne Wanne-Eickel Hbf'; 'Neuss Hbf' ==
    'Neuss Hauptbahnhof'. Both disagreements are systematic between IRIS and
    DELFI (findings §13).
    """
    key = _NON_ALNUM.sub("", normalize_name(name))
    return _SUFFIX.sub("", key) or key


def query_variants(name: str, ds100: str | None = None) -> list[str]:
    """Query strings to try against /station/, best first."""
    out: list[str] = []
    if ds100:
        out.append(ds100)
    cleaned = name.strip()
    out.append(cleaned)

    # 'S Ostkreuz Bhf (Berlin)' -> 'Berlin Ostkreuz'.
    # The trailing-suffix strip must run AFTER the parenthesised city is lifted
    # off, or the suffix is never at the end and never matches.
    stripped = re.sub(r"^(S\+U|S|U|Bf)\s+", "", cleaned)
    match = _PAREN_CITY.match(stripped)
    if match:
        core, city = match.group(1), match.group(2)
        core_bare = _TRAILING_BF.sub("", core)
        out.append(f"{city} {core_bare}".strip())
        out.append(f"{city} {core}".strip())
        stripped = core
    stripped = _TRAILING_BF.sub("", stripped)
    if stripped != cleaned:
        out.append(stripped)
    # 'Hamburg, Hamburg-Altona' -> 'Hamburg Hamburg-Altona'
    if "," in cleaned:
        out.append(cleaned.replace(",", " ").strip())
        out.append(cleaned.split(",")[0].strip())
    seen: set[str] = set()
    return [q for q in out if q and not (q in seen or seen.add(q))]


@dataclass(slots=True)
class EvaCandidate:
    eva: int
    name: str
    ds100: str | None
    query: str
    name_match: bool

    @property
    def accepted(self) -> bool:
        return self.eva in RAIL_EVA_RANGE and self.name_match


def resolve_eva(client: TimetablesClient, station_name: str) -> tuple[EvaCandidate | None, list[str]]:
    """Try each query variant until a railway eva with a matching name appears.

    Returns (best candidate or None, notes explaining what was rejected).
    """
    notes: list[str] = []
    target = loose_name(station_name)
    fallback: EvaCandidate | None = None

    for query in query_variants(station_name):
        resp = client.station(query)
        if not resp.ok:
            notes.append(f"{query!r}: HTTP {resp.status}")
            continue
        try:
            found = parse_stations(resp.body)
        except Exception as exc:  # malformed XML is a finding, not a crash
            notes.append(f"{query!r}: unparseable ({exc})")
            continue
        if not found:
            notes.append(f"{query!r}: no match")
            continue

        rail = [s for s in found if s["eva"] and s["eva"] in RAIL_EVA_RANGE]
        if not rail:
            names = ", ".join(f"{s['name']}({s['eva']})" for s in found[:3])
            notes.append(f"{query!r}: only non-rail evas, rejected: {names}")
            continue

        for candidate in rail:
            cand = EvaCandidate(
                eva=candidate["eva"],
                name=candidate["name"] or "",
                ds100=candidate["ds100"],
                query=query,
                name_match=loose_name(candidate["name"] or "") == target,
            )
            if cand.name_match:
                return cand, notes
            fallback = fallback or cand
        notes.append(
            f"{query!r}: {len(rail)} rail hit(s) but no name match "
            f"(got {rail[0]['name']!r}, wanted {station_name!r})"
        )

    return None, notes + ([f"best unverified: {fallback.name!r} eva={fallback.eva}"] if fallback else [])


def map_stations(
    session: Session,
    client: TimetablesClient,
    station_ids: list[int],
    *,
    progress=lambda msg: None,
) -> dict[int, EvaCandidate]:
    """Resolve and persist an eva for each station. Only accepted matches are saved."""
    stations = session.execute(select(Station).where(Station.id.in_(station_ids))).scalars().all()
    accepted: dict[int, EvaCandidate] = {}
    for station in stations:
        cand, notes = resolve_eva(client, station.name)
        if cand is None:
            progress(f"  {station.name}: UNRESOLVED")
            for note in notes:
                progress(f"      {note}")
            continue
        station.eva = cand.eva
        station.ds100 = cand.ds100
        station.eva_source = "iris"
        accepted[station.id] = cand
        progress(f"  {station.name}: eva={cand.eva} ds100={cand.ds100} via {cand.query!r}")
    session.flush()
    return accepted


def track_stations(session: Session, station_ids: list[int], *, priority: int = 1) -> int:
    """Add stations with a resolved eva to the tracked set."""
    now = int(time.time())
    added = 0
    for station in session.execute(select(Station).where(Station.id.in_(station_ids))).scalars():
        if station.eva is None or station.tracked:
            continue
        station.tracked = 1
        station.poll_priority = priority
        station.tracked_at = now
        added += 1
    session.flush()
    return added


def gtfs_neighbours(session: Session, station_id: int) -> set[str]:
    """Loose names of the stations immediately before/after this one, per GTFS.

    Empty when the pattern tables are not loaded, in which case the behavioural
    check is skipped rather than silently passing.
    """
    rows = session.execute(
        select(StationStop.train_id, StationStop.stop_sequence).where(
            StationStop.station_id == station_id
        )
    ).all()
    if not rows:
        return set()
    wanted = {(train_id, seq + delta) for train_id, seq in rows for delta in (-1, 1)}
    if not wanted:
        return set()
    train_ids = {t for t, _ in wanted}
    neighbour_rows = session.execute(
        select(StationStop.train_id, StationStop.stop_sequence, Station.name)
        .join(Station, Station.id == StationStop.station_id)
        .where(StationStop.train_id.in_(list(train_ids)[:5000]))
    ).all()
    return {loose_name(name) for tid, seq, name in neighbour_rows if (tid, seq) in wanted}


def verify_station(
    session: Session, client: TimetablesClient, station: Station, *, hour: int | None = None
) -> tuple[float | None, str]:
    """Behavioural check: do IRIS's neighbours for this station match GTFS's?

    Returns (overlap or None if not checkable, explanation).
    """
    expected = gtfs_neighbours(session, station.id)
    if not expected:
        return None, "no GTFS pattern data loaded; behavioural check skipped"

    now = time.localtime()
    hour = hour if hour is not None else now.tm_hour
    date = time.strftime("%y%m%d", now)
    resp = client.plan(station.eva, date, hour)
    if not resp.ok:
        return None, f"/plan returned {resp.status}"
    parsed = parse_timetable(resp.body)
    if not parsed.stops:
        return None, "/plan returned no stops for this hour"

    seen: set[str] = set()
    for stop in parsed.stops:
        if stop.arrival:
            names = stop.arrival.path_names()
            if names:
                seen.add(loose_name(names[-1]))
        if stop.departure:
            names = stop.departure.path_names()
            if names:
                seen.add(loose_name(names[0]))
    if not seen:
        return None, "no ppth neighbours in the sample"
    overlap = len(seen & expected) / len(seen)
    return overlap, f"{len(seen & expected)}/{len(seen)} IRIS neighbours known to GTFS"
