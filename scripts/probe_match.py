"""Probe gtfs.de static + realtime feeds to answer the questions the schema depends on.

Standalone: no database, no project imports. Run before writing any schema code.

    uv run python scripts/probe_match.py            # fv + rv
    uv run python scripts/probe_match.py --feed fv  # long distance only

Caches downloads under data/probe/ so re-runs are cheap.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
from google.transit import gtfs_realtime_pb2

STATIC_URLS = {
    "fv": "https://download.gtfs.de/germany/fv_free/latest.zip",
    "rv": "https://download.gtfs.de/germany/rv_free/latest.zip",
}
RT_URL = "https://realtime.gtfs.de/realtime-free.pb"
CACHE = Path("data/probe")
IFOPT_RE = re.compile(r"^de:\d+:\d+")


def hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def fetch(url: str, name: str, max_age_s: float) -> bytes:
    """Download url, reusing the cached copy if it is younger than max_age_s."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_s:
        age = int(time.time() - path.stat().st_mtime)
        print(f"  using cached {path} ({path.stat().st_size:,} bytes, {age}s old)")
        return path.read_bytes()
    print(f"  downloading {url} ...", flush=True)
    t0 = time.monotonic()
    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        resp = client.get(url, headers={"Accept-Encoding": "gzip"})
        resp.raise_for_status()
        data = resp.content
    print(
        f"  {len(data):,} bytes in {time.monotonic() - t0:.1f}s"
        f"  etag={resp.headers.get('etag')}"
        f"  last-modified={resp.headers.get('last-modified')}"
        f"  content-encoding={resp.headers.get('content-encoding', 'none')}"
    )
    path.write_bytes(data)
    return data


def read_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    if name not in zf.namelist():
        return []
    with zf.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")))


def gtfs_time_to_sec(value: str) -> int | None:
    """'25:30:00' -> 91800. Returns None for blank/malformed values."""
    parts = value.strip().split(":")
    if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
        return None
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def weekday_matches(row: dict[str, str], date: str) -> bool:
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    weekday = datetime.strptime(date, "%Y%m%d").weekday()
    return row.get(days[weekday]) == "1"


def active_trip_ids(zf: zipfile.ZipFile, trips: list[dict[str, str]], date: str) -> set[str]:
    """Trip ids running on `date` (YYYYMMDD), per calendar.txt + calendar_dates.txt."""
    services: set[str] = set()
    for row in read_csv(zf, "calendar.txt"):
        if row["start_date"] <= date <= row["end_date"] and weekday_matches(row, date):
            services.add(row["service_id"])
    for row in read_csv(zf, "calendar_dates.txt"):
        if row["date"] != date:
            continue
        if row["exception_type"] == "1":
            services.add(row["service_id"])
        elif row["exception_type"] == "2":
            services.discard(row["service_id"])
    return {t["trip_id"] for t in trips if t["service_id"] in services}


def trip_spans(zf: zipfile.ZipFile, wanted: set[str]) -> dict[str, tuple[int, int]]:
    """trip_id -> (first departure sec, last arrival sec), for trips in `wanted`."""
    spans: dict[str, tuple[int, int]] = {}
    for row in read_csv(zf, "stop_times.txt"):
        tid = row["trip_id"]
        if tid not in wanted:
            continue
        dep = gtfs_time_to_sec(row.get("departure_time", "") or row.get("arrival_time", ""))
        arr = gtfs_time_to_sec(row.get("arrival_time", "") or row.get("departure_time", ""))
        if dep is None or arr is None:
            continue
        lo, hi = spans.get(tid, (dep, arr))
        spans[tid] = (min(lo, dep), max(hi, arr))
    return spans


def probe_static(feed: str, date: str) -> dict:
    hr(f"STATIC FEED: {feed}")
    raw = fetch(STATIC_URLS[feed], f"{feed}.zip", max_age_s=6 * 3600)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    print(f"  files: {', '.join(sorted(zf.namelist()))}")

    trips = read_csv(zf, "trips.txt")
    stops = read_csv(zf, "stops.txt")
    routes = read_csv(zf, "routes.txt")
    stop_times = read_csv(zf, "stop_times.txt")
    print(f"\n  trips={len(trips):,}  stops={len(stops):,}  routes={len(routes):,}  stop_times={len(stop_times):,}")

    print(f"\n  trips.txt columns: {list(trips[0].keys())}")
    print(f"  stops.txt columns: {list(stops[0].keys())}")
    print(f"  routes.txt columns: {list(routes[0].keys())}")
    print(f"  stop_times.txt columns: {list(stop_times[0].keys())}")

    print("\n  -- sample trip_ids --")
    for t in trips[:8]:
        print(f"    {t['trip_id']!r}  route={t.get('route_id')!r}  short_name={t.get('trip_short_name')!r}  headsign={t.get('trip_headsign')!r}")

    print("\n  -- sample stop_ids --")
    for s in stops[:8]:
        print(f"    {s['stop_id']!r}  {s.get('stop_name')!r}  parent={s.get('parent_station')!r}  loc_type={s.get('location_type')!r}")

    print("\n  -- sample route_ids --")
    for r in routes[:8]:
        print(f"    {r['route_id']!r}  short={r.get('route_short_name')!r}  long={r.get('route_long_name')!r}  type={r.get('route_type')!r}")

    # Q: is stop_id IFOPT/DHID-shaped? The station natural key depends on this.
    ifopt = sum(1 for s in stops if IFOPT_RE.match(s["stop_id"]))
    print(f"\n  stop_id matches IFOPT 'de:N:N' : {ifopt:,}/{len(stops):,} ({ifopt / len(stops):.1%})")

    # Q: does trip_short_name carry the train number? The train identity depends on this.
    has_short = sum(1 for t in trips if t.get("trip_short_name", "").strip())
    print(f"  trips with non-empty trip_short_name: {has_short:,}/{len(trips):,} ({has_short / len(trips):.1%})")

    # Q: do times past 24:00:00 exist? Confirms the seconds-from-anchor decision.
    max_sec, max_raw = -1, ""
    over24 = 0
    for st in stop_times:
        for key in ("arrival_time", "departure_time"):
            sec = gtfs_time_to_sec(st.get(key, ""))
            if sec is None:
                continue
            if sec >= 86400:
                over24 += 1
            if sec > max_sec:
                max_sec, max_raw = sec, st[key]
    print(f"  max stop_time value: {max_raw!r} ({max_sec}s);  values >= 24:00:00: {over24:,}")

    # Calendar shape + service date window.
    cal, cal_dates = read_csv(zf, "calendar.txt"), read_csv(zf, "calendar_dates.txt")
    print(f"\n  calendar.txt rows={len(cal):,}   calendar_dates.txt rows={len(cal_dates):,}")
    all_dates = [r["date"] for r in cal_dates] + [r["start_date"] for r in cal] + [r["end_date"] for r in cal]
    if all_dates:
        print(f"  service date range: {min(all_dates)} .. {max(all_dates)}")

    active = active_trip_ids(zf, trips, date)
    print(f"  trips active on {date}: {len(active):,}")

    route_type = {r["route_id"]: r.get("route_type") for r in routes}
    return {
        "feed": feed,
        "all_trip_ids": {t["trip_id"] for t in trips},
        "active_trip_ids": active,
        "spans": trip_spans(zf, active),
        "trip_route_type": {t["trip_id"]: route_type.get(t["route_id"]) for t in trips},
        "stop_ids": {s["stop_id"] for s in stops},
        "route_ids": {r["route_id"] for r in routes},
        "sample_trip_ids": [t["trip_id"] for t in trips[:8]],
    }


def probe_realtime(max_age_s: float) -> dict:
    hr("REALTIME FEED")
    raw = fetch(RT_URL, "realtime.pb", max_age_s=max_age_s)
    feed = gtfs_realtime_pb2.FeedMessage()
    t0 = time.monotonic()
    feed.ParseFromString(raw)
    print(f"  parsed in {time.monotonic() - t0:.2f}s")

    header = feed.header
    age = time.time() - header.timestamp if header.timestamp else float("nan")
    print(f"\n  gtfs_realtime_version={header.gtfs_realtime_version!r}")
    print(f"  incrementality={header.incrementality} (0=FULL_DATASET, 1=DIFFERENTIAL)")
    print(f"  header.timestamp={header.timestamp} ({datetime.fromtimestamp(header.timestamp, UTC).isoformat()}) age={age:.0f}s")

    kinds = Counter()
    trip_updates = []
    for entity in feed.entity:
        if entity.HasField("trip_update"):
            kinds["trip_update"] += 1
            trip_updates.append(entity.trip_update)
        if entity.HasField("alert"):
            kinds["alert"] += 1
        if entity.HasField("vehicle"):
            kinds["vehicle"] += 1
    print(f"  entities={len(feed.entity):,}  {dict(kinds)}")

    # Q: how are stop_time_updates keyed, and which fields are populated?
    keyed = Counter()
    fields = Counter()
    n_updates = 0
    updates_per_trip = []
    passed_stop_trips = 0
    now = time.time()
    for tu in trip_updates:
        stus = list(tu.stop_time_update)
        updates_per_trip.append(len(stus))
        if any(
            (stu.HasField("arrival") and stu.arrival.time and stu.arrival.time < now - 300)
            or (stu.HasField("departure") and stu.departure.time and stu.departure.time < now - 300)
            for stu in stus
        ):
            passed_stop_trips += 1
        for stu in stus:
            n_updates += 1
            has_seq, has_sid = stu.HasField("stop_sequence"), bool(stu.stop_id)
            keyed["both" if has_seq and has_sid else "stop_sequence" if has_seq else "stop_id" if has_sid else "neither"] += 1
            for side in ("arrival", "departure"):
                if not stu.HasField(side):
                    continue
                ev = getattr(stu, side)
                fields[f"{side}.time" if ev.HasField("time") else f"{side}.no_time"] += 1
                fields[f"{side}.delay" if ev.HasField("delay") else f"{side}.no_delay"] += 1

    print(f"\n  stop_time_updates total={n_updates:,}")
    print(f"  keyed by: {dict(keyed)}")
    print(f"  field population: {dict(sorted(fields.items()))}")
    if updates_per_trip:
        s = sorted(updates_per_trip)
        print(f"  updates/trip: min={s[0]} p50={s[len(s) // 2]} p90={s[int(len(s) * 0.9)]} max={s[-1]}")
    print(f"  trips retaining already-passed stops (>5min old): {passed_stop_trips:,}/{len(trip_updates):,}")
    print("    ^ high => stops stay in the feed after passage (OBSERVED grade dominates)")
    print("      low  => the feed prunes passed stops (INFERRED_DOWNSTREAM dominates)")

    print("\n  -- sample TripDescriptors --")
    for tu in trip_updates[:8]:
        t = tu.trip
        print(f"    trip_id={t.trip_id!r} route_id={t.route_id!r} start_date={t.start_date!r} start_time={t.start_time!r} rel={t.schedule_relationship}")

    rel = Counter(tu.trip.schedule_relationship for tu in trip_updates)
    print(f"\n  trip schedule_relationship: {dict(rel)} (0=SCHEDULED 1=ADDED 2=UNSCHEDULED 3=CANCELED)")
    with_date = sum(1 for tu in trip_updates if tu.trip.start_date)
    with_id = sum(1 for tu in trip_updates if tu.trip.trip_id)
    print(f"  trip_updates with trip_id={with_id:,}/{len(trip_updates):,}  with start_date={with_date:,}/{len(trip_updates):,}")

    return {
        "feed_timestamp": header.timestamp or int(time.time()),
        "trip_ids": {tu.trip.trip_id for tu in trip_updates if tu.trip.trip_id},
        "trip_ids_with_updates": {tu.trip.trip_id for tu in trip_updates if tu.trip.trip_id and len(tu.stop_time_update) > 0},
        "route_ids": {tu.trip.route_id for tu in trip_updates if tu.trip.route_id},
        "stop_ids": {stu.stop_id for tu in trip_updates for stu in tu.stop_time_update if stu.stop_id},
        "n_trip_updates": len(trip_updates),
        "sample_trip_ids": [tu.trip.trip_id for tu in trip_updates[:8]],
        "delays": {
            tu.trip.trip_id: {
                stu.stop_sequence: (
                    stu.arrival.delay if stu.HasField("arrival") else None,
                    stu.departure.delay if stu.HasField("departure") else None,
                )
                for stu in tu.stop_time_update
            }
            for tu in trip_updates
            if tu.trip.trip_id
        },
    }


def report_match(static: dict, rt: dict) -> float:
    """Coverage of the realtime feed, measured against the population it can cover.

    The feed is a rolling window: it carries trips already running or starting
    shortly, not the whole service day. Scoring against every trip active today
    therefore understates coverage by ~4x. The meaningful denominator is trips
    in progress at the feed's own timestamp.
    """
    feed = static["feed"]
    hr(f"MATCH RATE: {feed} static  x  realtime")
    active = static["active_trip_ids"]
    spans = static["spans"]
    matched = active & rt["trip_ids"]

    local = datetime.fromtimestamp(rt["feed_timestamp"]).astimezone()
    now_sec = local.hour * 3600 + local.minute * 60 + local.second
    in_progress = {t for t in active if t in spans and spans[t][0] <= now_sec <= spans[t][1]}
    covered = in_progress & rt["trip_ids_with_updates"]
    rate = len(covered) / len(in_progress) if in_progress else 0.0

    print(f"  feed timestamp (local)      : {local:%Y-%m-%d %H:%M:%S %Z}")
    print(f"  static trips active today   : {len(active):,}")
    print(f"  ... present in realtime      : {len(matched):,} ({len(matched) / max(len(active), 1):.1%}) — understates: most have not departed yet")
    print(f"\n  trips IN PROGRESS right now  : {len(in_progress):,}")
    print(f"  ... with >=1 stop_time_update: {len(covered):,} ({rate:.1%})   <-- THE NUMBER")
    print(f"\n  realtime trips matching this feed: {len(static['all_trip_ids'] & rt['trip_ids']):,}/{len(rt['trip_ids']):,}"
          f" (the rest are bus/tram/other modes)")

    if matched:
        types = Counter(static["trip_route_type"].get(t) for t in matched)
        print(f"  matched by route_type: {dict(types)}")
        offsets = sorted(spans[t][0] - now_sec for t in matched if t in spans)
        print(f"  matched first_departure - now: {offsets[0] / 3600:+.1f}h .. {offsets[-1] / 3600:+.1f}h"
              f"  (the feed's look-ahead window)")

    if rate < 0.2:
        print("\n  !! LOW MATCH — diagnosing with independent id-space overlap")
        print(f"  route_id overlap: {len(static['route_ids'] & rt['route_ids']):,}/{len(static['route_ids']):,} static routes")
        print(f"  stop_id  overlap: {len(static['stop_ids'] & rt['stop_ids']):,}/{len(static['stop_ids']):,} static stops")
        print("\n  static trip_id samples : " + ", ".join(repr(x) for x in static["sample_trip_ids"][:5]))
        print("  realtime trip_id samples: " + ", ".join(repr(x) for x in rt["sample_trip_ids"][:5]))
        print("\n  If stop_ids overlap but trip_ids do not, the project is still buildable")
        print("  on (route, start_time) matching -- but that is a different plan.")

    verdict = "PASS" if rate > 0.7 else "MARGINAL" if rate >= 0.2 else "FAIL"
    print(f"\n  GATE [{feed}]: {verdict}  ({rate:.1%}; pass >70%, marginal 20-70%, fail <20%)")
    return rate


def report_drift(before: dict, after: dict, static_sets: list[dict]) -> None:
    """Second realtime sample: how much actually changes, and do trips vanish?"""
    hr("TEMPORAL BEHAVIOUR (second realtime sample)")
    ours: set[str] = set()
    for s in static_sets:
        ours |= s["active_trip_ids"]

    b_trips = before["trip_ids"] & ours
    a_trips = after["trip_ids"] & ours
    print(f"  our trips in sample 1: {len(b_trips):,}")
    print(f"  our trips in sample 2: {len(a_trips):,}")
    print(f"  vanished: {len(b_trips - a_trips):,}   appeared: {len(a_trips - b_trips):,}")

    common = b_trips & a_trips
    changed_events = unchanged_events = 0
    changed_trips = 0
    for tid in common:
        b, a = before["delays"].get(tid, {}), after["delays"].get(tid, {})
        trip_changed = False
        for seq in set(b) & set(a):
            if b[seq] == a[seq]:
                unchanged_events += 1
            else:
                changed_events += 1
                trip_changed = True
        changed_trips += trip_changed
    total = changed_events + unchanged_events
    print(f"  common trips: {len(common):,}, of which changed: {changed_trips:,}")
    print(f"  stop events compared: {total:,}  changed: {changed_events:,} ({changed_events / max(total, 1):.1%})")
    print("    ^ this is the change-only write rate the storage estimate depends on")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feed", default="fv,rv", help="comma-separated: fv,rv")
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="service date YYYYMMDD")
    ap.add_argument("--drift-wait", type=int, default=0, help="seconds to wait before a 2nd realtime sample (0=skip)")
    args = ap.parse_args()

    feeds = [f.strip() for f in args.feed.split(",") if f.strip()]
    print(f"probe date: {args.date}   feeds: {feeds}")

    statics = [probe_static(f, args.date) for f in feeds]
    rt = probe_realtime(max_age_s=300)
    rates = [report_match(s, rt) for s in statics]

    if args.drift_wait > 0:
        print(f"\nwaiting {args.drift_wait}s for a second realtime sample ...", flush=True)
        time.sleep(args.drift_wait)
        rt2 = probe_realtime(max_age_s=0)
        report_drift(rt, rt2, statics)

    hr("VERDICT")
    best = max(rates) if rates else 0.0
    for feed, rate in zip(feeds, rates, strict=True):
        print(f"  {feed}: {rate:.1%}")
    if best > 0.7:
        print("\n  PASS — proceed with the planned schema.")
        return 0
    if best >= 0.2:
        print("\n  MARGINAL — proceed, but investigate the unmatched share.")
        return 0
    print("\n  FAIL — stop and rethink id matching before writing schema.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
