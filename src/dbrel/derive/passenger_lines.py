"""Regroup trains into the lines a passenger actually knows (models.PassengerLine).

`service_line` is keyed by category + line text, which gets lines wrong both
ways: every "RE" + "4" in Germany shares one row (Schwerin-Szczecin, Aachen and
Lake Constance alike), while the one Aachen-Dortmund RE4 is spread over "RE/4",
"NX/RE4" and "RE/RE4" because operators write the category and line
differently. Here trains are grouped by their normalised line name ("RE 4" and
"RE4" are one name) across every service_line row, then split into regional
corridors: two routes are one corridor once they share at least 2 stations
(the direction code's rule, direction._SHARED_NAME_THRESHOLD), counted by
name OR by station id, and each corridor is one passenger line. Names alone
miss real overlaps -- IRIS spells one stop "Berlin Gesundbrunnen" in one ppth
and "Gesundbrunnen Bahnhof (S+U), Berlin" in another -- which left e.g. the
Hamburg S5 and Berlin's S41/S42 in pieces.

Full rebuild every run, like derive/stop_averages.py. Line ids are the smallest
member train id, so a line keeps its URL across rebuilds as long as that train
stays in it.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from types import SimpleNamespace

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from dbrel.direction import _SHARED_NAME_THRESHOLD, canonical_order
from dbrel.identity import SOURCE_TT
from dbrel.ingest.runs import TT_FEED_KIND
from dbrel.models import (
    FeedVersion,
    PassengerLine,
    PassengerLineMember,
    ServiceLine,
    StationStop,
    Train,
)

# A service_line whose "line" is just a generic product code -- IRIS echoing
# the train category ("RE", "RB", "S", ...) where a real line name belongs. It
# names no actual route, so it is never offered as a line. Operator codes used
# as a line name (HSB, MBB, SAB, FEX, SEV, P, ...) are real named services and
# are deliberately NOT in this set.
BARE_CATEGORY_LINES = frozenset(
    {"RE", "RB", "S", "R", "RS", "E", "EX", "D", "IR", "IRE", "IC", "ICE", "EC", "ECE",
     "EN", "NJ", "BUS"}
)


def is_bare_category_line(line_text: str | None) -> bool:
    return (line_text or "").strip().upper() in BARE_CATEGORY_LINES


def normalise_line_name(name: str) -> str:
    """'RE 4' and 'RE4' are one line name."""
    return re.sub(r"\s+", "", name).upper()


_NAME_NOISE = re.compile(r"\((?:s|u|s\+u|s-bahn)\)|\bbahnhof\b|\bbf\b")


def stop_key(name: str) -> frozenset[str]:
    """One stop however IRIS spells it in a ppth: "Hamburg, Neugraben" and
    "Hamburg-Neugraben", "Berlin Gesundbrunnen" and "Gesundbrunnen Bahnhof
    (S+U), Berlin", "Hamburg Hbf (S-Bahn)" and "Hamburg Hbf" -- the words,
    without the noise, in any order."""
    text = name.lower().replace("hauptbahnhof", "hbf")
    text = _NAME_NOISE.sub(" ", text)
    return frozenset(word for word in re.split(r"[^\wäöüß]+", text) if word)


def _corridors(paths: list[str], station_ids: dict[str, set[int]]) -> list[list[str]]:
    """Union-find over route paths of one line name. Two paths are joined when
    they share >= 2 stops (by spelling-insensitive name, or by station id), or
    when one ends where the other begins: IRIS often files one through run as
    two pieces meeting at a single stop (Hamburg S5: Elbgaustraße-Neugraben
    and Neugraben-Stade). Only paths already sharing a token are compared."""
    parent = {p: p for p in paths}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    names = {p: {stop_key(part) for part in p.split("|") if part} for p in paths}
    ends = {}
    for p in paths:
        stops = [stop_key(part) for part in p.split("|") if part]
        ends[p] = {stops[0], stops[-1]} if stops else set()
    index: dict[object, list[str]] = {}
    for p in paths:
        for name in names[p]:
            index.setdefault(("n", name), []).append(p)
        for sid in station_ids.get(p, ()):
            index.setdefault(("s", sid), []).append(p)

    checked: set[tuple[str, str]] = set()
    for bucket in index.values():
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                if find(a) == find(b):
                    continue
                key = (a, b) if a < b else (b, a)
                if key in checked:
                    continue
                checked.add(key)
                if (
                    len(names[a] & names[b]) >= _SHARED_NAME_THRESHOLD
                    or ends[a] & ends[b]
                    or len(station_ids.get(a, set()) & station_ids.get(b, set()))
                    >= _SHARED_NAME_THRESHOLD
                ):
                    parent[find(a)] = find(b)

    groups: dict[str, list[str]] = {}
    for p in paths:
        groups.setdefault(find(p), []).append(p)
    return list(groups.values())


def build_passenger_lines(session: Session) -> dict:
    started = time.time()
    rows = session.execute(
        select(
            Train.id,
            Train.route_path,
            ServiceLine.display_name,
            ServiceLine.line,
            ServiceLine.category,
        )
        .join(ServiceLine, ServiceLine.id == Train.line_id)
        .where(
            Train.source == SOURCE_TT,
            (Train.segment.is_(None)) | (Train.segment != "FV"),
        )
    ).all()

    # (normalised name, is replacement bus) -> route_path -> member train ids.
    # A replacement bus signed "RB22" runs the same stations as the RB22 trains,
    # but is a different service to a passenger, so it is kept apart.
    groups: dict[tuple[str, bool], dict[str, list[int]]] = {}
    names: dict[int, str] = {}
    categories: dict[int, str] = {}
    skipped = 0
    for train_id, route_path, display_name, line_text, category in rows:
        if not display_name or is_bare_category_line(line_text) or not route_path:
            skipped += 1
            continue
        key = (normalise_line_name(display_name), (category or "").upper() == "BUS")
        groups.setdefault(key, {}).setdefault(route_path, []).append(train_id)
        names[train_id] = display_name
        categories[train_id] = category

    # The stations each train is timetabled at, by station id -- on the route
    # variant its route_path describes (train.path_sha1) only. A train number
    # can run elsewhere on other days (OE 93232: Frankfurt(Oder)-Berlin, and a
    # variant via Schwerin), and taking every variant's stations bridged the
    # Berlin and Mecklenburg RE1s into one line.
    stations_of_train: dict[int, set[int]] = {}
    tts = session.execute(
        select(FeedVersion.id).where(FeedVersion.kind == TT_FEED_KIND)
    ).scalar_one_or_none()
    if tts is not None:
        for train_id, station_id in session.execute(
            select(StationStop.train_id, StationStop.station_id)
            .join(Train, Train.id == StationStop.train_id)
            .where(
                StationStop.feed_version_id == tts,
                StationStop.path_sha1 == Train.path_sha1,
            )
            .distinct()
        ):
            if train_id in names:
                stations_of_train.setdefault(train_id, set()).add(station_id)

    lines: list[dict] = []
    members: list[dict] = []
    for (name_norm, _is_bus), by_path in groups.items():
        # Trains sharing a route_path are clustered once, not once per train.
        path_stations = {
            path: set().union(*(stations_of_train.get(t, set()) for t in train_ids))
            for path, train_ids in by_path.items()
        }
        for component in _corridors(list(by_path), path_stations):
            train_ids = [t for path in component for t in by_path[path]]
            canonical = canonical_order([SimpleNamespace(route_path=p) for p in component])
            line_id = min(train_ids)
            lines.append(
                {
                    "id": line_id,
                    "name": Counter(names[t] for t in train_ids).most_common(1)[0][0],
                    "name_norm": name_norm,
                    "category": Counter(categories[t] for t in train_ids).most_common(1)[0][0],
                    "endpoints": (
                        f"{canonical[0]} – {canonical[-1]}" if len(canonical) >= 2 else None
                    ),
                    "n_trains": len(train_ids),
                }
            )
            members.extend({"train_id": t, "passenger_line_id": line_id} for t in train_ids)

    session.execute(delete(PassengerLineMember))
    session.execute(delete(PassengerLine))
    if lines:
        session.execute(PassengerLine.__table__.insert(), lines)
        for start in range(0, len(members), 50_000):
            session.execute(PassengerLineMember.__table__.insert(), members[start : start + 50_000])
    return {
        "lines": len(lines),
        "trains": len(members),
        "skipped": skipped,
        "names": len(groups),
        "elapsed_s": time.time() - started,
    }
