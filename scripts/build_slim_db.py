"""Build the slim database a deployed server reads: schedule, trains, stations, search.

The 48 GB database holds every observed trip; serving a forecast needs only the timetable
tables, the recent runs that pick a train's route variant, and the model's notice-rate totals
(a few hundred thousand rows instead of the 13 M-row daily table). The source is opened
read-only; only the output file is written.

    dbrel ml launch build-slim --memory 4G -- env PYTHONPATH=src .venv/bin/python scripts/build_slim_db.py

Then copy the file to the server's volume (see fly.toml / README "Deploying").
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402

from dbrel.config import DB_PATH, ML_MODELS_DIR  # noqa: E402
from dbrel.db import make_engine  # noqa: E402
from dbrel.ml.bakeoff import MANIFEST_NAME, current_artifact  # noqa: E402
from dbrel.ml.notice_rates import NoticeRates, uses_notice_rates, write_totals  # noqa: E402
from dbrel.models import Base  # noqa: E402

# Copied whole. stop_delay_average and train_stop_delay_average are empty today but the web path
# queries them, so their (empty) tables must exist.
TABLES = ["feed_version", "public_holiday", "route", "service_line", "station", "train", "station_stop",
          "search_alias", "passenger_line", "passenger_line_member", "stop_delay_average",
          "train_stop_delay_average"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=DB_PATH)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "deploy" / "slim.sqlite")
    parser.add_argument("--trip-days", type=int, default=60,
                        help="days of runs kept (only used to pick a train's most recent route variant)")
    args = parser.parse_args()

    artifact = current_artifact(ML_MODELS_DIR)
    manifest = json.loads((artifact / MANIFEST_NAME).read_text()) if artifact else {}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.unlink(missing_ok=True)
    engine = create_engine(f"sqlite:///{args.out}")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[t] for t in TABLES])
    engine.dispose()  # release the file before the plain sqlite3 connection below writes to it

    con = sqlite3.connect(f"file:{args.out.resolve()}", uri=True)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(f"ATTACH DATABASE 'file:{Path(args.source).resolve()}?mode=ro' AS src")

    def step(label: str, sql: str, params=()) -> None:
        started = time.monotonic()
        rows = con.execute(sql, params).rowcount
        con.commit()
        print(f"{label}: {rows} rows, {time.monotonic() - started:.0f}s", flush=True)

    for table in TABLES:
        cols = ", ".join(f'"{r[1]}"' for r in con.execute(f"PRAGMA main.table_info({table})"))
        step(table, f'INSERT INTO main."{table}" ({cols}) SELECT {cols} FROM src."{table}"')

    con.execute("CREATE TABLE trip (id INTEGER PRIMARY KEY, train_id INTEGER NOT NULL, "
                "service_date TEXT NOT NULL, path_sha1 TEXT)")
    (last,) = con.execute("SELECT MAX(service_date) FROM src.trip").fetchone()
    step(f"trip (last {args.trip_days} days to {last})",
         f"INSERT INTO trip SELECT id, train_id, service_date, path_sha1 FROM src.trip "
         f"WHERE service_date > date(?, '-{args.trip_days} days')", (last,))
    con.execute("CREATE INDEX ix_trip_train ON trip (train_id, path_sha1, service_date)")
    con.commit()
    con.execute("DETACH DATABASE src")
    con.close()

    if uses_notice_rates(manifest):
        started = time.monotonic()
        record = manifest["notice_rates"]
        rates = NoticeRates.from_record(make_engine(args.source), record)  # refuses if the counts changed
        n = write_totals(engine, rates)
        print(f"notice_rate_total: {n} rows, {time.monotonic() - started:.0f}s "
              f"(fingerprint {record['fingerprint'][:12]} verified)", flush=True)
    engine.dispose()
    vacuum = sqlite3.connect(args.out)
    vacuum.execute("VACUUM")
    vacuum.close()
    print(f"{args.out}: {args.out.stat().st_size / 2**20:.0f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
