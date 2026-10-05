"""Check that a deployed database answers exactly as the collection database (dbrel.deployed_compare).

    dbrel ml launch compare-deployed --memory 4G -- env PYTHONPATH=src .venv/bin/python \\
        scripts/compare_deployed_db.py --deployed data/deploy/slim.sqlite

Exits 1 and lists the differences (request, JSON path, both values) if there are any.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy.orm import Session

from dbrel.config import DB_PATH
from dbrel.db import make_engine
from dbrel.deployed_compare import compare, requests_for, sample
from dbrel.ingest.timeutil import local_date, service_date_anchor


def main() -> int:
    """Run the parity check from the command line; returns 0 with no differences, else 1."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--collection", type=Path, default=DB_PATH)
    parser.add_argument("--deployed", type=Path, required=True)
    parser.add_argument("--stations", type=int, default=20, help="random stations, besides the 3 busiest")
    parser.add_argument("--trains", type=int, default=20, help="random trains, besides the 5 with most variants")
    parser.add_argument("--lines", type=int, default=10)
    parser.add_argument("--corridors", type=int, default=5, help="random trains without a line")
    parser.add_argument("--station", type=int, action="append", default=[], help="also this station id")
    parser.add_argument("--train", type=int, action="append", default=[], help="also this train id")
    parser.add_argument("--line", type=int, action="append", default=[], help="also this line id")
    parser.add_argument("--term", action="append", default=[], help="also search for this")
    parser.add_argument("--date", type=date.fromisoformat, default=local_date(int(time.time())),
                        help="service date for boards and runs-around (default: today in Germany)")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    collection, deployed = make_engine(args.collection), make_engine(args.deployed)
    now = service_date_anchor(args.date) + 9 * 3600  # boards at 09:00 local on that date
    with Session(deployed) as s:
        picked = sample(s, n_stations=args.stations, n_trains=args.trains, n_lines=args.lines,
                        n_corridors=args.corridors, seed=args.seed)
        picked["stations"] += args.station
        picked["trains"] += args.train
        picked["lines"] += args.line
        picked["terms"] += args.term
        requests = requests_for(s, now=now, service_date=args.date, **picked)
    print(f"{len(requests)} requests: {len(picked['stations'])} stations, {len(picked['trains'])} trains, "
          f"{len(picked['lines'])} lines, {len(picked['corridors'])} line-less trains, "
          f"{len(picked['terms'])} search terms", flush=True)
    started = time.monotonic()
    differences = compare(collection, deployed, requests, log=lambda m: print(m, flush=True))
    print(f"{len(differences)} differences in {time.monotonic() - started:.0f}s")
    for line in differences[:50]:
        print("  " + line)
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
