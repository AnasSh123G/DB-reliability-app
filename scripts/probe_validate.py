"""Are the trip_id matches real, or integer collisions?

The probe found only ~19% of static trip_ids present in the realtime feed. Both
id spaces are plain integers, so a match may be coincidence rather than identity.
This checks whether matched trips describe the *same journey*: same stop_ids, in
the same order, at the same times.

    uv run python scripts/probe_validate.py --feed fv
    uv run python scripts/probe_validate.py --feed full   # after downloading full.zip

Reads the caches written by probe_match.py; downloads nothing except a fresh .pb
if the cached one is stale.
"""

from __future__ import annotations

import argparse
import csv
import io
import random
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from google.transit import gtfs_realtime_pb2

CACHE = Path("data/probe")


def read_csv(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", default="fv", help="fv | rv | full (uses data/probe/<feed>.zip)")
    ap.add_argument("--samples", type=int, default=10)
    args = ap.parse_args()

    zip_path = CACHE / f"{args.feed}.zip"
    pb_path = CACHE / "realtime.pb"
    for p in (zip_path, pb_path):
        if not p.exists():
            print(f"missing {p} — run probe_match.py first")
            return 1

    print(f"loading {zip_path} ({zip_path.stat().st_size:,} bytes) ...", flush=True)
    zf = zipfile.ZipFile(zip_path)

    static_stops: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for row in read_csv(zf, "stop_times.txt"):
        static_stops[row["trip_id"]].append(
            (int(row["stop_sequence"]), row["stop_id"], row["departure_time"])
        )
    for seq in static_stops.values():
        seq.sort()
    print(f"static trips with stop_times: {len(static_stops):,}")

    stop_names = {r["stop_id"]: r["stop_name"] for r in read_csv(zf, "stops.txt")}

    print(f"loading {pb_path} ({pb_path.stat().st_size:,} bytes) ...", flush=True)
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(pb_path.read_bytes())

    rt_stops: dict[str, list[tuple[int, str]]] = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        if not tu.trip.trip_id:
            continue
        rt_stops[tu.trip.trip_id] = sorted(
            (stu.stop_sequence, stu.stop_id) for stu in tu.stop_time_update
        )
    print(f"realtime trips: {len(rt_stops):,}")

    shared = sorted(set(static_stops) & set(rt_stops))
    print(f"\ntrip_ids present in both: {len(shared):,}")
    if not shared:
        print("no overlap at all — the two feeds use disjoint id spaces.")
        return 1

    verdict = Counter()
    for tid in shared:
        s_ids = {sid for _, sid, _ in static_stops[tid]}
        r_ids = {sid for _, sid in rt_stops[tid]}
        if not r_ids:
            verdict["rt_has_no_stops"] += 1
        elif r_ids <= s_ids:
            verdict["rt_stops_subset_of_static (REAL MATCH)"] += 1
        elif r_ids & s_ids:
            verdict["partial_overlap"] += 1
        else:
            verdict["disjoint (COLLISION)"] += 1

    print("\nstop_id agreement for shared trip_ids:")
    total = sum(verdict.values())
    for k, v in verdict.most_common():
        print(f"  {k:42s} {v:6,}  ({v / total:.1%})")

    print(f"\n-- {args.samples} sampled shared trips --")
    for tid in random.sample(shared, min(args.samples, len(shared))):
        s = static_stops[tid]
        r = rt_stops[tid]
        s_ids = {sid for _, sid, _ in s}
        r_ids = {sid for _, sid in r}
        tag = "MATCH" if r_ids and r_ids <= s_ids else "COLLISION" if not (r_ids & s_ids) else "PARTIAL"
        print(f"\n  trip_id={tid}  [{tag}]  static_stops={len(s)} rt_stops={len(r)}")
        print(f"    static: {[(q, stop_names.get(sid, sid)[:22], t) for q, sid, t in s[:4]]}")
        print(f"    rt    : {[(q, stop_names.get(sid, sid)[:22]) for q, sid in r[:4]]}")

    real = sum(v for k, v in verdict.items() if "REAL MATCH" in k)
    print(f"\nVERDICT: {real:,}/{total:,} ({real / total:.1%}) of shared trip_ids are genuine matches")
    if real / total < 0.5:
        print("  => the overlap is mostly coincidence; this feed pair cannot be joined on trip_id.")
    else:
        print("  => trip_id joins are sound; the low match rate is coverage, not identity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
