"""Probe the DB Timetables API (IRIS-TTS) to answer the questions the schema depends on.

Standalone: no database, no project imports. Run before writing any schema code.
Mirrors scripts/probe_match.py in spirit — measure first, then design.

    uv run python scripts/probe_timetables.py check     # P3  credentials + rate limit
    uv run python scripts/probe_timetables.py stations  # resolve EVAs for the pilot set
    uv run python scripts/probe_timetables.py plan      # P2 P4 P5 P6 P7 P8 P9
    uv run python scripts/probe_timetables.py watch     # P1  (long running, ~90 min)
    uv run python scripts/probe_timetables.py all       # check + stations + plan

Caches every raw response under data/probe/tt/ so re-runs are cheap and the exact
bytes behind a finding stay auditable.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

BASE_URL = os.environ.get(
    "DBREL_TT_BASE_URL",
    "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1",
)
CACHE = Path("data/probe/tt")
RV_ZIP = Path("data/feeds/rv.zip")
FV_ZIP = Path("data/feeds/fv.zip")

# The pilot set, as gtfs.de names them. Each entry lists query variants to try
# against /station/, best first — the shape of this list is itself a finding,
# because it is what clean_station_name() will have to reproduce in the real code.
#
# "Herne Wanne-Eickel Hbf" is the cautionary case: IRIS spells it
# "Herne-Wanne-Eickel Hbf" (extra hyphen), so the name search misses entirely, and
# the near-miss query "Wanne-Eickel" returns a *bus stop* at eva 212342 rather than
# the Hbf at 8000192. Only the DS100 code finds it. Hence RAIL_EVA_RANGE below.
PILOT_STATIONS: list[tuple[str, list[str]]] = [
    ("Bochum Hbf", ["Bochum Hbf"]),
    ("Dortmund Hbf", ["Dortmund Hbf"]),
    ("Essen Hbf", ["Essen Hbf"]),
    ("Gelsenkirchen Hbf", ["Gelsenkirchen Hbf"]),
    ("Herne Wanne-Eickel Hbf", ["EWAN", "Herne-Wanne-Eickel Hbf", "Wanne-Eickel Hbf"]),
    ("Hagen Hbf", ["Hagen Hbf"]),
    ("Herne Bf", ["Herne", "Herne Bf"]),
    ("Castrop-Rauxel Hbf", ["Castrop-Rauxel Hbf", "Castrop-Rauxel"]),
]

# German long-distance/regional railway stations sit in 8000000-8099999. Six-digit
# evas are DELFI stop ids for bus/tram halts, which /station/ will happily return
# for a name query and which would silently poison a tracked station.
RAIL_EVA_RANGE = range(8_000_000, 8_100_000)

RPM = int(os.environ.get("DBREL_TT_RPM", "50"))


def hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Populate os.environ from .env. Real env vars win, so a shell export overrides."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def credentials() -> tuple[str | None, str | None, str, str]:
    """Return (client_id, api_key, id_source, key_source). Never print the values."""
    for name in ("DBREL_TT_CLIENT_ID", "DB_CLIENT_ID", "DB_CLIENT_ID_"):
        if os.environ.get(name):
            client_id, id_source = os.environ[name], name
            break
    else:
        client_id, id_source = None, "<unset>"
    for name in ("DBREL_TT_API_KEY", "TIMETABLES_API_KEY", "DB_API_KEY"):
        if os.environ.get(name):
            api_key, key_source = os.environ[name], name
            break
    else:
        api_key, key_source = None, "<unset>"
    return client_id, api_key, id_source, key_source


class Probe:
    """Minimal rate-limited client. Sleeps to stay under RPM; caches raw bytes."""

    def __init__(self, *, rpm: int = RPM, use_cache: bool = True) -> None:
        self.client_id, self.api_key, self.id_source, self.key_source = credentials()
        self.min_interval = 60.0 / rpm
        self.last_call = 0.0
        self.use_cache = use_cache
        self.n_requests = 0
        self.statuses: Counter[int] = Counter()
        self.http = httpx.Client(timeout=30.0, follow_redirects=True)
        CACHE.mkdir(parents=True, exist_ok=True)

    def headers(self, *, with_id: bool = True, with_key: bool = True) -> dict[str, str]:
        h = {"Accept": "application/xml"}
        if with_id and self.client_id:
            h["DB-Client-Id"] = self.client_id
        if with_key and self.api_key:
            h["DB-Api-Key"] = self.api_key
        return h

    def get(
        self, path: str, *, cache_name: str | None = None, **header_flags
    ) -> tuple[int, bytes, httpx.Headers]:
        if cache_name and self.use_cache:
            cached = CACHE / cache_name
            if cached.exists():
                return 200, cached.read_bytes(), httpx.Headers()

        wait = self.min_interval - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.monotonic()

        resp = self.http.get(BASE_URL + path, headers=self.headers(**header_flags))
        self.n_requests += 1
        self.statuses[resp.status_code] += 1

        if resp.status_code == 429:
            retry = float(resp.headers.get("Retry-After", "5"))
            print(f"    429, sleeping {retry}s", flush=True)
            time.sleep(retry)
            return self.get(path, cache_name=cache_name, **header_flags)

        if cache_name and resp.status_code == 200:
            (CACHE / cache_name).write_bytes(resp.content)
        return resp.status_code, resp.content, resp.headers


# --------------------------------------------------------------------------
# P3 — credentials and rate limit
# --------------------------------------------------------------------------


def probe_check(p: Probe, burst: int = 0) -> dict[str, int] | None:
    hr("P3  Credentials and rate limit")
    print(f"  client id from {p.id_source}  ({len(p.client_id or '')} chars)")
    print(f"  api key   from {p.key_source}  ({len(p.api_key or '')} chars)")
    if not p.client_id or not p.api_key:
        print("  MISSING credential - cannot continue")
        return None
    if len(p.client_id) > len(p.api_key):
        print("  WARNING: client id is longer than the api key; values may be swapped")

    print(f"\n  {'headers sent':<28} {'status':<8} body[:90]")
    print(f"  {'-' * 28} {'-' * 8} {'-' * 90}")
    combos = [
        ("DB-Client-Id + DB-Api-Key", dict(with_id=True, with_key=True)),
        ("DB-Api-Key only", dict(with_id=False, with_key=True)),
        ("DB-Client-Id only", dict(with_id=True, with_key=False)),
        ("neither", dict(with_id=False, with_key=False)),
    ]
    working = None
    for label, flags in combos:
        status, body, headers = p.get("/station/BLS", **flags)
        snippet = body[:90].decode("utf-8", "replace").replace("\n", " ")
        print(f"  {label:<28} {status:<8} {snippet}")
        if status == 200 and working is None:
            working = label
            for k, v in headers.items():
                if "ratelimit" in k.lower() or "retry" in k.lower():
                    print(f"      {k}: {v}")

    print(f"\n  -> authenticates with: {working or 'NOTHING - check the subscription'}")

    if burst and working:
        hr(f"P3  Burst test: {burst} requests as fast as allowed")
        t0 = time.monotonic()
        saved, p.min_interval = p.min_interval, 0.0
        codes: Counter[int] = Counter()
        for i in range(burst):
            status, _, headers = p.get("/station/BLS")
            codes[status] += 1
            if status != 200:
                print(f"    request {i + 1}: {status} after {time.monotonic() - t0:.1f}s")
                for k, v in headers.items():
                    if "ratelimit" in k.lower() or "retry" in k.lower():
                        print(f"      {k}: {v}")
                break
        p.min_interval = saved
        print(f"  {burst} requests in {time.monotonic() - t0:.1f}s -> {dict(codes)}")
    return dict(p.statuses)


# --------------------------------------------------------------------------
# Station resolution
# --------------------------------------------------------------------------


def probe_stations(p: Probe) -> dict[str, int]:
    hr("Station resolution: pilot set -> EVA")
    resolved: dict[str, int] = {}
    print(f"  {'gtfs.de name':<26} {'query that worked':<24} {'eva':<9} {'ds100':<7} IRIS name")
    print(f"  {'-' * 26} {'-' * 24} {'-' * 9} {'-' * 7} {'-' * 24}")
    for gtfs_name, variants in PILOT_STATIONS:
        hit = None
        for variant in variants:
            safe = re.sub(r"[^A-Za-z0-9]+", "_", variant)
            status, body, _ = p.get(f"/station/{variant}", cache_name=f"station_{safe}.xml")
            if status != 200:
                continue
            stations = ET.fromstring(body).findall("station")
            if not stations:
                continue
            # Discard non-railway evas outright: a name query happily returns bus
            # halts, and accepting one is the silent failure this whole step exists
            # to prevent.
            rail = [s for s in stations if int(s.get("eva", "0")) in RAIL_EVA_RANGE]
            if not rail:
                names = ", ".join(f"{s.get('name')}({s.get('eva')})" for s in stations[:3])
                print(f"  ! {variant!r} matched only non-rail evas, rejected: {names}")
                continue
            # Prefer an exact case-insensitive name match, else the first result.
            exact = [s for s in rail if s.get("name", "").casefold() == variant.casefold()]
            node = (exact or rail)[0]
            hit = (variant, node)
            if len(rail) > 1:
                others = ", ".join(s.get("name", "?") for s in rail[1:4])
                print(f"  ! {variant!r} was ambiguous ({len(rail)} rail hits): {others}")
            break
        if hit is None:
            print(f"  {gtfs_name:<26} {'NO MATCH':<24}")
            continue
        variant, node = hit
        eva = int(node.get("eva", "0"))
        resolved[gtfs_name] = eva
        print(
            f"  {gtfs_name:<26} {variant:<24} {eva:<9} "
            f"{node.get('ds100', '') or '':<7} {node.get('name', '')}"
        )

    (CACHE / "pilot_evas.json").write_text(json.dumps(resolved, indent=2, ensure_ascii=False))
    print(f"\n  resolved {len(resolved)}/{len(PILOT_STATIONS)} -> {CACHE / 'pilot_evas.json'}")
    return resolved


# --------------------------------------------------------------------------
# The plan sweep — P2 P4 P5 P6 P7 P8 P9
# --------------------------------------------------------------------------


def parse_stop(elem: ET.Element) -> dict:
    tl = elem.find("tl")
    ar, dp = elem.find("ar"), elem.find("dp")
    return {
        "id": elem.get("id", ""),
        "eva": elem.get("eva"),
        "c": tl.get("c") if tl is not None else None,
        "n": tl.get("n") if tl is not None else None,
        "o": tl.get("o") if tl is not None else None,
        "t": tl.get("t") if tl is not None else None,
        "f": tl.get("f") if tl is not None else None,
        "ar": dict(ar.attrib) if ar is not None else None,
        "dp": dict(dp.attrib) if dp is not None else None,
        "has_ref": elem.find("ref") is not None,
        "messages": [dict(m.attrib) for m in elem.findall("m")],
        "raw": ET.tostring(elem, encoding="unicode"),
    }


def split_stop_id(sid: str) -> tuple[str, str, int] | None:
    """'-787...321-1403311221-11' -> ('-787...321', '1403311221', 11). rsplit, not split."""
    try:
        daily, ts, idx = sid.rsplit("-", 2)
        return daily, ts, int(idx)
    except ValueError:
        return None


def normalize_name(name: str) -> str:
    """Same shape as static_import.normalize_name: casefold + strip accents."""
    folded = unicodedata.normalize("NFKD", name.casefold())
    return "".join(c for c in folded if not unicodedata.combining(c)).strip()


# gtfs.de appends product markers to some station names ("Essen Steele S",
# "Herne Bf"); IRIS does not. Strip them before comparing.
_SUFFIX_RE = re.compile(r"(bf|bhf|s|bahnhof)$")


def loose_name(name: str) -> str:
    """Punctuation-insensitive key: 'Herne-Wanne-Eickel Hbf' == 'Herne Wanne-Eickel Hbf'."""
    key = re.sub(r"[^a-z0-9]+", "", normalize_name(name))
    stripped = _SUFFIX_RE.sub("", key)
    return stripped or key


def gtfs_station_names() -> set[str]:
    """Parent-station names from the cached gtfs.de feeds (for P4)."""
    names: set[str] = set()
    for zip_path in (FV_ZIP, RV_ZIP):
        if not zip_path.exists():
            continue
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open("stops.txt") as fh:
                reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))
                for row in reader:
                    if row.get("location_type") == "1" and row.get("stop_name"):
                        names.add(row["stop_name"])
    return names


def probe_plan(p: Probe, evas: dict[str, int], date: str, hours: list[int]) -> None:
    hr(f"Plan sweep: {len(evas)} stations x {len(hours)} hours on {date}")
    stops: list[dict] = []
    missing_hours = 0
    for name, eva in evas.items():
        got = 0
        for hour in hours:
            status, body, _ = p.get(
                f"/plan/{eva}/{date}/{hour:02d}",
                cache_name=f"plan_{eva}_{date}_{hour:02d}.xml",
            )
            if status != 200:
                missing_hours += 1
                continue
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                missing_hours += 1
                continue
            for elem in root.findall("s"):
                s = parse_stop(elem)
                s["station"] = name
                stops.append(s)
                got += 1
        print(f"  {name:<26} {got:>5} stops")
    print(f"\n  {len(stops):,} stop events total; {missing_hours} hour buckets missing/failed")
    if not stops:
        print("  nothing to analyse")
        return

    # ---- P5 tl@n presence and shape -------------------------------------
    hr("P5  Is tl@n always present and numeric?")
    no_n = [s for s in stops if not s["n"]]
    non_numeric = [s for s in stops if s["n"] and not s["n"].isdigit()]
    print(f"  stops with no tl@n     : {len(no_n):,} / {len(stops):,}")
    print(f"  stops with non-numeric : {len(non_numeric):,}")
    if non_numeric:
        print(f"    examples: {sorted({s['n'] for s in non_numeric})[:10]}")
    widths = Counter(len(s["n"]) for s in stops if s["n"])
    print(f"  tl@n length histogram  : {dict(sorted(widths.items()))}")
    print(f"  -> {'Integer' if not non_numeric else 'String'} is the right column type")
    cats = Counter(s["c"] for s in stops)
    print(f"  categories: {dict(cats.most_common(15))}")

    # ---- P2 (category, number) uniqueness per day ------------------------
    hr("P2  Is (tl@c, tl@n) unique per day?")
    by_cn: dict[tuple, set[str]] = defaultdict(set)
    for s in stops:
        parsed = split_stop_id(s["id"])
        if parsed and s["n"]:
            by_cn[(s["c"], s["n"])].add(parsed[0])
    collisions = {k: v for k, v in by_cn.items() if len(v) > 1}
    print(f"  distinct (category, number) : {len(by_cn):,}")
    print(f"  with >1 daily trip id       : {len(collisions):,}")
    for (cat, num), ids in list(collisions.items())[:8]:
        print(f"    {cat} {num}: {len(ids)} distinct trip ids")
    if collisions:
        print("  -> operator or path MUST disambiguate; revisit the identity key")
    else:
        print("  -> (category, number) is safe as the cross-day identity key")

    # ---- P6 stopIndex vs ppth length -------------------------------------
    hr("P6  Is stopIndex the trip-wide stop ordinal? (and is it 0- or 1-based?)")
    offsets: Counter[int] = Counter()
    examples = []
    origin_indices: Counter[int] = Counter()
    for s in stops:
        parsed = split_stop_id(s["id"])
        if not parsed:
            continue
        if not s["ar"] or not s["ar"].get("ppth"):
            origin_indices[parsed[2]] += 1  # no arrival = origin of the run
            continue
        n_before = len(s["ar"]["ppth"].split("|"))
        offsets[parsed[2] - n_before] += 1
        if parsed[2] - n_before not in (0, 1) and len(examples) < 5:
            examples.append((s["id"], parsed[2], n_before))
    print(f"  stopIndex - len(ar@ppth) histogram : {dict(sorted(offsets.items()))}")
    print(f"  index at origin stops (no ar)      : {dict(sorted(origin_indices.items()))}")
    for sid, idx, n_before in examples:
        print(f"    anomaly {sid}  index={idx} ppth={n_before}")
    if set(offsets) == {1}:
        print("  -> stopIndex IS the trip-wide ordinal, 1-BASED. Use it directly as")
        print("     stop_sequence; the invariant is len(ar@ppth) == stopIndex - 1.")
    elif set(offsets) == {0}:
        print("  -> stopIndex is the trip-wide ordinal, 0-based.")
    else:
        print("  -> inconsistent; derive stop_sequence from ppth position instead")

    # ---- id middle field width (the swagger prose/example disagree) ------
    hr("Stop id: width of the middle field")
    widths = Counter(len(split_stop_id(s["id"])[1]) for s in stops if split_stop_id(s["id"]))
    print(f"  histogram: {dict(sorted(widths.items()))}  (6=YYMMdd, 10=YYMMddHHmm)")
    sample = next((s["id"] for s in stops if s["id"].startswith("-")), None)
    print(f"  example with negative trip id: {sample}")

    # ---- P7 dp@l line coverage -------------------------------------------
    hr("P7  Is dp@l present, and constant along a trip?")
    by_cat_line: dict[str, Counter] = defaultdict(Counter)
    for s in stops:
        line = (s["dp"] or {}).get("l") or (s["ar"] or {}).get("l")
        by_cat_line[s["c"] or "?"]["with l" if line else "no l"] += 1
    print(f"  {'category':<10} {'with l':>8} {'no l':>8}")
    for cat, counts in sorted(by_cat_line.items(), key=lambda kv: -sum(kv[1].values()))[:12]:
        print(f"  {cat:<10} {counts['with l']:>8} {counts['no l']:>8}")
    per_run: dict[str, set[str]] = defaultdict(set)
    for s in stops:
        parsed = split_stop_id(s["id"])
        line = (s["dp"] or {}).get("l") or (s["ar"] or {}).get("l")
        if parsed and line:
            per_run[parsed[0]].add(line)
    varying = {k: v for k, v in per_run.items() if len(v) > 1}
    print(f"  runs whose l varies between stops: {len(varying):,} / {len(per_run):,}")

    # ---- P4 ppth name resolution -----------------------------------------
    hr("P4  Do ppth station names resolve to gtfs.de station names?")
    known_raw = gtfs_station_names()
    if not known_raw:
        print(f"  no cached feed at {RV_ZIP} - skipping")
    else:
        strict = {normalize_name(n) for n in known_raw}
        loose = {loose_name(n) for n in known_raw}
        tokens: Counter[str] = Counter()
        for s in stops:
            for event in (s["ar"], s["dp"]):
                if event and event.get("ppth"):
                    for token in event["ppth"].split("|"):
                        tokens[token.strip()] += 1
        total = sum(tokens.values())
        print(f"  gtfs.de parent stations loaded : {len(known_raw):,}")
        print(f"  distinct ppth names            : {len(tokens):,}")
        for label, keyfn, table in (
            ("strict (casefold+accents)", normalize_name, strict),
            ("loose  (punctuation-free)", loose_name, loose),
        ):
            hit = {t for t in tokens if keyfn(t) in table}
            weighted = sum(c for t, c in tokens.items() if t in hit)
            print(
                f"  {label} : {len(hit):>4}/{len(tokens)} distinct "
                f"({100 * len(hit) / len(tokens):5.1f}%), "
                f"{100 * weighted / total:5.1f}% weighted"
            )
        misses = [(t, c) for t, c in tokens.most_common() if loose_name(t) not in loose]
        print(f"\n  still unresolved after loose matching ({len(misses):,} distinct):")
        for token, count in misses[:25]:
            print(f"    {count:>6}x  {token}")

    # ---- P8 number changes ------------------------------------------------
    hr("P8  Number changes: <ref>, tra, tl@t")
    with_ref = [s for s in stops if s["has_ref"]]
    with_tra = [s for s in stops if (s["ar"] or {}).get("tra") or (s["dp"] or {}).get("tra")]
    with_wings = [s for s in stops if (s["ar"] or {}).get("wings") or (s["dp"] or {}).get("wings")]
    print(f"  stops with <ref>  : {len(with_ref):,}")
    print(f"  stops with @tra   : {len(with_tra):,}")
    print(f"  stops with @wings : {len(with_wings):,}")
    print(f"  tl@t histogram    : {dict(Counter(s['t'] for s in stops))}")
    print(f"  tl@f histogram    : {dict(Counter(s['f'] for s in stops))}")
    for s in (with_ref + with_tra + with_wings)[:3]:
        print(f"\n  --- raw <s> with ref/tra/wings ---\n{s['raw'][:1200]}")

    # ---- planned status / platforms --------------------------------------
    hr("Planned-side extras")
    print(f"  ar@ps values : {dict(Counter((s['ar'] or {}).get('ps') for s in stops))}")
    print(f"  dp@ps values : {dict(Counter((s['dp'] or {}).get('ps') for s in stops))}")
    n_platform = sum(1 for s in stops if (s["dp"] or {}).get("pp") or (s["ar"] or {}).get("pp"))
    print(f"  stops with a planned platform: {n_platform:,} / {len(stops):,}")
    print(f"  stops carrying <m> messages  : {sum(1 for s in stops if s['messages']):,}")


def probe_plan_horizon(p: Probe, eva: int) -> None:
    """P9 - how far ahead does /plan serve data? Measured by hour, not by day.

    This sets the /plan lead time and decides whether a whole service day can ever
    be pre-fetched. It is also the reason MISSING detection needs the GTFS
    skeleton: anything not captured before the horizon passes is gone for good.
    """
    hr("P9  /plan horizon, hour by hour")
    now = datetime.now(UTC)
    first_ok = last_ok = None
    for offset in range(-4, 40, 2):
        moment = now + timedelta(hours=offset)
        status, body, _ = p.get(f"/plan/{eva}/{moment:%y%m%d}/{moment:%H}")
        n = 0
        if status == 200:
            try:
                n = len(ET.fromstring(body).findall("s"))
            except ET.ParseError:
                n = -1
        if status == 200:
            first_ok = offset if first_ok is None else first_ok
            last_ok = offset
        print(f"  {moment:%Y-%m-%d %H}:00  ({offset:+3d} h) -> {status}, {n} stops")
    if first_ok is not None:
        print(f"\n  -> usable window: {first_ok:+d} h to {last_ok:+d} h from now")
        print("     A /plan bucket must be fetched inside this window or it is lost.")


# --------------------------------------------------------------------------
# P1 — the fchg watch loop
# --------------------------------------------------------------------------


def probe_watch(p: Probe, evas: dict[str, int], minutes: int, interval: int) -> None:
    """P1 — the question the PLAN_HELD grade depends on.

    Fetches today's /plan for the window we are about to watch, then polls /fchg
    across it. The decisive number is what share of *planned* stop events ever
    show up in /fchg at all: if most punctual trains never appear, then absence
    under observation means ON TIME, not "no data".
    """
    hr(f"P1  /fchg semantics: {len(evas)} stations for {minutes} min every {interval}s")

    # --- the denominator: planned stops in the window -----------------------
    now = datetime.now(UTC)
    hours = sorted({(now + timedelta(hours=h)).strftime("%y%m%d/%H") for h in range(0, 3)})
    planned: dict[str, dict] = {}
    for name, eva in evas.items():
        for slot in hours:
            status, body, _ = p.get(f"/plan/{eva}/{slot}", cache_name=None)
            if status != 200:
                continue
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                continue
            for elem in root.findall("s"):
                s = parse_stop(elem)
                for side in ("ar", "dp"):
                    if s[side]:
                        planned[f"{s['id']}|{side}"] = {
                            "station": name,
                            "pt": s[side].get("pt"),
                            "cat": s["c"],
                            "n": s["n"],
                        }
    print(f"  planned stop events in the next ~3 h: {len(planned):,}")

    first_seen: dict[str, float] = {}
    last_seen: dict[str, float] = {}
    values: dict[str, list[tuple[float, str]]] = defaultdict(list)
    seen_ct: set[str] = set()
    noop_changes = 0
    cancels = 0
    n_sweeps = 0
    deadline = time.monotonic() + minutes * 60

    while time.monotonic() < deadline:
        sweep_start = time.monotonic()
        n_sweeps += 1
        total = 0
        for name, eva in evas.items():
            status, body, _ = p.get(f"/fchg/{eva}")
            if status != 200:
                print(f"    {name}: status {status}")
                continue
            if n_sweeps == 1:
                (CACHE / f"fchg_{eva}_sample.xml").write_bytes(body)
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                continue
            stamp = time.time()
            for elem in root.findall("s"):
                s = parse_stop(elem)
                total += 1
                for side in ("ar", "dp"):
                    event = s[side]
                    if not event:
                        continue
                    key = f"{s['id']}|{side}"
                    signature = json.dumps(
                        {k: event.get(k) for k in ("ct", "cp", "cs", "clt", "cpth")},
                        sort_keys=True,
                    )
                    first_seen.setdefault(key, stamp)
                    last_seen[key] = stamp
                    if not values[key] or values[key][-1][1] != signature:
                        values[key].append((stamp, signature))
                    if event.get("ct"):
                        seen_ct.add(key)
                        if event.get("pt") == event.get("ct"):
                            noop_changes += 1
                    if event.get("cs") == "c":
                        cancels += 1
        print(
            f"  sweep {n_sweeps:>3}: {total:>5} change-stops, "
            f"{len(values):>6} distinct events seen, {time.monotonic() - sweep_start:.1f}s",
            flush=True,
        )
        wait = interval - (time.monotonic() - sweep_start)
        if wait > 0 and time.monotonic() + wait < deadline:
            time.sleep(wait)

    hr("P1  Results")
    print(f"  sweeps                          : {n_sweeps}")
    print(f"  distinct events ever in /fchg   : {len(values):,}")
    print(f"  events carrying a ct            : {len(seen_ct):,}")
    print(f"  no-op observations (ct == pt)   : {noop_changes:,}")
    print(f"  cancellation flags (cs='c')     : {cancels:,}")
    revised = sum(1 for v in values.values() if len(v) > 1)
    print(f"  events whose value changed      : {revised:,}")

    if planned:
        appeared = sum(1 for k in planned if k in values)
        with_ct = sum(1 for k in planned if k in seen_ct)
        print(f"\n  Of {len(planned):,} PLANNED events in the window:")
        print(f"    ever appeared in /fchg      : {appeared:,} ({100 * appeared / len(planned):.1f}%)")
        print(f"    ever carried a ct           : {with_ct:,} ({100 * with_ct / len(planned):.1f}%)")
        silent = len(planned) - appeared
        print(f"    NEVER appeared (silent)     : {silent:,} ({100 * silent / len(planned):.1f}%)")
        if silent > len(planned) * 0.2:
            print("\n  -> A large silent population confirms /fchg publishes only changes.")
            print("     PLAN_HELD is REQUIRED: grading these NO_DATA would discard most")
            print("     of the on-time population and skew every statistic late.")
        else:
            print("\n  -> Almost everything appears in /fchg; PLAN_HELD may be unnecessary.")
            print("     Re-read this before implementing finalize 5.2.")

    lingers = sorted((last_seen[k] - first_seen[k]) / 60 for k in first_seen)
    if lingers:
        mid = lingers[len(lingers) // 2]
        p90 = lingers[int(len(lingers) * 0.9)]
        print(
            f"\n  linger in /fchg (min)           : median {mid:.1f}, "
            f"p90 {p90:.1f}, max {lingers[-1]:.1f}"
        )
        print("  (this bounds the achievable SETTLE_S)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["check", "stations", "plan", "watch", "all"], nargs="?", default="all"
    )
    parser.add_argument("--burst", type=int, default=0, help="P3: fire N requests as fast as allowed")
    parser.add_argument("--date", default=None, help="plan date YYMMdd (default: tomorrow)")
    parser.add_argument("--hours", default="5-22", help="plan hours, e.g. 5-22 or 8,9,17")
    parser.add_argument("--minutes", type=int, default=90, help="watch duration")
    parser.add_argument("--interval", type=int, default=30, help="watch sweep interval (s)")
    parser.add_argument("--no-cache", action="store_true", help="ignore cached responses")
    args = parser.parse_args()

    load_dotenv()
    p = Probe(use_cache=not args.no_cache)

    if args.mode in ("check", "all"):
        if probe_check(p, args.burst) is None:
            return 1

    evas: dict[str, int] = {}
    eva_file = CACHE / "pilot_evas.json"
    if args.mode in ("stations", "all") or not eva_file.exists():
        evas = probe_stations(p)
    elif eva_file.exists():
        evas = {k: int(v) for k, v in json.loads(eva_file.read_text()).items()}

    if not evas and args.mode in ("plan", "watch"):
        print("No EVAs resolved; run `stations` first.")
        return 1

    if args.mode in ("plan", "all"):
        if "-" in args.hours:
            lo, hi = args.hours.split("-")
            hours = list(range(int(lo), int(hi) + 1))
        else:
            hours = [int(h) for h in args.hours.split(",")]
        date = args.date or f"{(datetime.now(UTC) + timedelta(days=1)):%y%m%d}"
        probe_plan(p, evas, date, hours)
        probe_plan_horizon(p, next(iter(evas.values())))

    if args.mode == "watch":
        probe_watch(p, evas, args.minutes, args.interval)

    hr("Request budget")
    print(f"  {p.n_requests} requests issued, statuses {dict(p.statuses)}")
    print(f"  raw responses cached under {CACHE}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
