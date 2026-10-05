"""Build the deployed database a server reads: schedule, trains, stations, search.

The 48 GB collection database holds every observed run; serving a forecast needs only the
timetable, the route-variant run summary and the model's notice-rate totals. The schedule is
stored as shared stop patterns behind a `station_stop` view that gives back every row exactly
(see `dbrel.deployed`), and the build checks every row against the source before it replaces the
output. The source is opened read-only; only the output file is written.

    dbrel ml launch build-slim --memory 4G -- env PYTHONPATH=src .venv/bin/python scripts/build_slim_db.py

Then copy the file to the server's volume (see fly.toml / README "Deploying").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbrel.config import DB_PATH, ML_MODELS_DIR
from dbrel.deployed_build import DeployedBuildError, build_deployed
from dbrel.ml.bakeoff import MANIFEST_NAME, current_artifact


def main() -> int:
    """Build the deployed database from the command line; returns 0 on success, 1 if refused."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=DB_PATH)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "deploy" / "slim.sqlite")
    args = parser.parse_args()

    artifact = current_artifact(ML_MODELS_DIR)
    manifest = json.loads((artifact / MANIFEST_NAME).read_text()) if artifact else None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        build_deployed(args.source, args.out, manifest=manifest, log=lambda m: print(m, flush=True))
    except DeployedBuildError as error:
        print(f"build refused, {args.out} left as it was: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
