"""Generate config/germany_stations.toml -- the national tracked-station seed.

Three facts drive the design, all measured on 2026-09-21:

1. `/station/*` returns the ENTIRE IRIS catalogue in one request (24,738 entries,
   7,258 of them in RAIL_EVA_RANGE), each with an authoritative eva, ds100 and
   IRIS-native name. That removes name-based eva resolution -- and with it the
   ~60% hit rate and wrong-city traps of findings SS13/SS14/SS22/SS25 -- from the
   national build entirely. Nothing in the repo knew this endpoint form worked.

2. IRIS publishes no coordinates, but `station.lat`/`lon` are NOT NULL and are ML
   features. StaDa (station-data/v2) supplies them keyed by eva -- a numeric join,
   so it cannot mis-resolve the way a name lookup can. Measured against the 210
   previously hand-verified stations: median 56 m, p90 150 m.

3. StaDa is DB Station&Service's managed list (5,408 stations), NOT a mirror of
   IRIS's catalogue, so it covers 74.7% of the rail evas. The shortfall is mostly
   tram-train and replacement-bus stops (Karlsruhe AVG, City-Bahn Chemnitz, Kassel
   RegioTram) plus zero-traffic operational points: 72% of the uncovered set has
   no service at all, and the whole of it is ~3.4% of national stop events.

Binding rule, and the reason this file is not just an eva dump: `natural_key`
embeds coordinates at three decimals (~110 m) while StaDa and gtfs.de disagree by
56 m at the median. Recomputing a key from StaDa coordinates would therefore miss
the existing gtfs.de row about as often as it hit it, and silently create a second
station row for one physical station. So an entry carries the EXISTING row's
natural_key when one was found, and lat/lon only so apply_station_seed() can
create a row for a station gtfs.de does not have at all.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.config import RAIL_EVA_RANGE, TT_API_KEY, TT_BASE_URL, TT_CLIENT_ID
from dbrel.db import get_engine
from dbrel.models import Station

H = {"DB-Client-Id": TT_CLIENT_ID, "DB-Api-Key": TT_API_KEY}
STADA = "https://apis.deutschebahn.com/db-api-marketplace/apis/station-data/v2/stations"
OUT = Path(__file__).resolve().parents[1] / "config" / "germany_stations.toml"

# How close a StaDa point must sit to a gtfs.de station before the two are taken
# to be the same place. Generous next to the 150 m p90, tight next to the ~1 km
# that separates genuinely distinct stations in one town.
BIND_METRES = 600.0


def canon(s: str) -> str:
    s = re.sub(r"[\s,]*\((zug|bus|tram|s)\)$", "", s, flags=re.I)
    s = re.sub(r"[\s,]+(bahnhof|bf|bhf|hp|haltepunkt)$", "", s, flags=re.I)
    s = re.sub(r"\bhauptbahnhof\b", "hbf", s, flags=re.I)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("ß", "ss")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def iris_catalogue(client: httpx.Client) -> list[dict]:
    r = client.get(f"{TT_BASE_URL}/station/*", timeout=60.0)
    r.raise_for_status()
    out = []
    for st in ET.fromstring(r.content).iter("station"):
        try:
            eva = int(st.attrib.get("eva", ""))
        except ValueError:
            continue
        if eva in RAIL_EVA_RANGE:
            out.append({"eva": eva, "name": st.attrib.get("name", ""),
                        "ds100": st.attrib.get("ds100") or None})
    return out


def stada_coordinates(client: httpx.Client) -> dict[int, tuple[float, float]]:
    coords: dict[int, tuple[float, float]] = {}
    offset, total = 0, None
    while total is None or offset < total:
        r = client.get(STADA, params={"limit": 1000, "offset": offset},
                       headers={"Accept": "application/json"}, timeout=60.0)
        r.raise_for_status()
        doc = r.json()
        total = doc["total"]
        for st in doc["result"]:
            for ev in st.get("evaNumbers") or []:
                pt = (ev.get("geographicCoordinates") or {}).get("coordinates")
                if pt:
                    coords[ev["number"]] = (pt[1], pt[0])  # GeoJSON is lon,lat
        offset += 1000
        time.sleep(0.05)
    return coords


def _day_traffic(eva: int) -> int:
    """Stops IRIS plans at this eva across a spread of hours. Zero means dead."""
    yymmdd = time.strftime("%y%m%d")
    total = 0
    with httpx.Client(headers=H, timeout=20.0) as c:
        for hh in ("08", "12", "17"):
            try:
                r = c.get(f"{TT_BASE_URL}/plan/{eva}/{yymmdd}/{hh}")
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                total += r.text.count("<s id=")
    return total


def _resolve_collisions(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep one eva per station row, and make it the one that carries trains.

    IRIS splits some stations across several evas for different platform groups --
    Berlin Hbf is 8011160 (ds100 BLS) and 8098160 (BL) at identical coordinates.
    Only the second has any service: the first plans zero stops. apply_station_seed
    refuses the second as a conflict once the first has claimed the row, so picking
    between them in file order silently tracked a dead eva for Germany's busiest
    station. Whichever eva actually plans trains wins instead.
    """
    # Group on the key the seed will actually land on: the bound row's key when
    # there is one, otherwise the key apply_station_seed derives from name and
    # position. Berlin Hbf's two evas have no gtfs row at all, so grouping only the
    # bound ones left them both in -- and they collide anyway, because identical
    # names at identical coordinates derive an identical key.
    from dbrel.ingest.static_import import station_natural_key

    by_key: dict[str, list[dict]] = {}
    for e in entries:
        key = e["natural_key"] or station_natural_key(e["name"], e["lat"], e["lon"])
        by_key.setdefault(key, []).append(e)

    kept, dropped = [], []
    for group in by_key.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        ranked = sorted(group, key=lambda e: (-_day_traffic(e["eva"]), e["eva"]))
        kept.append(ranked[0])
        dropped.extend(ranked[1:])
    kept.sort(key=lambda e: (e["name"], e["eva"]))
    return kept, dropped


def toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def main() -> None:
    with httpx.Client(headers=H) as client:
        print("fetching the IRIS station catalogue ...")
        iris = iris_catalogue(client)
        print(f"  {len(iris):,} stations in RAIL_EVA_RANGE")
        print("fetching StaDa coordinates ...")
        stada = stada_coordinates(client)
        print(f"  {len(stada):,} eva -> coordinate rows")

    with Session(get_engine()) as session:
        gtfs = session.execute(
            select(Station.id, Station.name, Station.lat, Station.lon, Station.natural_key)
        ).all()
    print(f"gtfs.de station rows in this database: {len(gtfs):,}")

    by_canon: dict[str, list] = {}
    for g in gtfs:
        by_canon.setdefault(canon(g.name), []).append(g)

    entries, no_coords, bound_by = [], [], {"coordinate": 0, "name": 0, "none": 0}
    for st in sorted(iris, key=lambda r: r["name"]):
        coords = stada.get(st["eva"])
        if coords is None:
            no_coords.append(st)
            continue
        lat, lon = coords

        # Bind to the existing gtfs.de row: position first (StaDa's eva join is
        # authoritative, so proximity is a stronger signal than any name rule),
        # then a unique canonical-name hit for rows whose coordinates drifted.
        candidates = by_canon.get(canon(st["name"]), [])
        near = [(metres(lat, lon, g.lat, g.lon), g) for g in candidates]
        near = [(d, g) for d, g in near if d <= BIND_METRES]
        near.sort(key=lambda p: p[0])
        if near:
            natural_key = near[0][1].natural_key
            bound_by["coordinate"] += 1
        elif len(candidates) == 1:
            natural_key = candidates[0].natural_key
            bound_by["name"] += 1
        else:
            natural_key = None
            bound_by["none"] += 1

        entries.append({"name": st["name"], "eva": st["eva"], "ds100": st["ds100"],
                        "natural_key": natural_key, "lat": lat, "lon": lon})

    entries, dropped = _resolve_collisions(entries)

    header = f'''# The national tracked-station seed for `dbrel collect`.
#
# Generated by scripts/build_national_seed.py -- see that file for why each field
# is here. Checked in deliberately: like config/ruhr_stations.toml, applying this
# needs ZERO live /station/ calls, so a cold start on another machine cannot
# re-run -- and re-fail -- the name resolution that findings SS13/SS14/SS22/SS25
# document as unreliable.
#
# Contents: {len(entries):,} of the {len(iris):,} stations IRIS lists in
# RAIL_EVA_RANGE. evas and names come from IRIS `/station/*`; coordinates from
# StaDa, joined on the eva number rather than on a name.
#
# The {len(no_coords):,} omitted stations are the ones StaDa does not carry. They are
# overwhelmingly tram-train and replacement-bus stops (Karlsruhe AVG, City-Bahn
# Chemnitz, Kassel RegioTram) and zero-traffic operational points: 72% of them run
# no service at all, and together they are ~3.4% of national stop events. Add them
# by hand here if a coordinate ever turns up for one.
#
# natural_key binds an entry to the station row import-static already created, so
# no duplicate row appears for a station gtfs.de also knows. Where it is absent,
# apply_station_seed() creates the row from lat/lon.

name = "Germany (national)"
priority = 1
'''

    lines = [header]
    for e in entries:
        lines.append("\n[[stations]]")
        lines.append(f'name = "{toml_escape(e["name"])}"')
        lines.append(f"eva = {e['eva']}")
        if e["ds100"]:
            lines.append(f'ds100 = "{toml_escape(e["ds100"])}"')
        if e["natural_key"]:
            lines.append(f'natural_key = "{toml_escape(e["natural_key"])}"')
        lines.append(f"lat = {e['lat']:.6f}")
        lines.append(f"lon = {e['lon']:.6f}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nwrote {OUT} with {len(entries):,} stations")
    print(f"  bound to an existing row by position : {bound_by['coordinate']:,}")
    print(f"  bound by unique canonical name       : {bound_by['name']:,}")
    print(f"  no existing row (will be created)    : {bound_by['none']:,}")
    print(f"  omitted, no StaDa coordinate         : {len(no_coords):,}")
    if dropped:
        print(f"  dropped as the quieter twin of a shared row: {len(dropped)}")
        for e in dropped:
            print(f"      {e['eva']} {e['ds100'] or '':8} {e['name']}")


if __name__ == "__main__":
    main()
