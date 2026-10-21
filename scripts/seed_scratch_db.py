#!/usr/bin/env python3
"""Create an empty replay database with reference data from a read-only source.

Keep reference ids, station tracking and poll priorities. Reset ingest state:
last_plan_hour and last_fchg_at become NULL; n_fchg_polls and n_fchg_errors
become zero. No ingest tables are copied. Existing outputs are never replaced.
Run this script in its own process so DBREL_DB is set before application imports.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3

TABLES = ("feed_version", "station", "route", "public_holiday")
STATION_RESETS = {
    "last_plan_hour": "NULL", "last_fchg_at": "NULL",
    "n_fchg_polls": "0", "n_fchg_errors": "0",
}


def _validate_paths(live: Path, out: Path) -> tuple[Path, Path]:
    source, target = live.resolve(), out.resolve()
    defaults = (
        Path(__file__).resolve().parents[1] / "data/dbrel.sqlite",
        Path("data/dbrel.sqlite"),
        Path(os.environ.get("DBREL_DATA_DIR", "data")) / "dbrel.sqlite",
    )
    if target == source or any(target == path.resolve() for path in defaults):
        raise ValueError(f"Refusing live database target: {target}")
    if out.exists() or out.is_symlink():
        raise FileExistsError(f"Output already exists: {out}")
    if not source.is_file():
        raise FileNotFoundError(f"Source database does not exist: {source}")
    return source, target


def _copy_reference_data(source: Path, target: Path) -> dict[str, int]:
    with sqlite3.connect(target.as_uri(), uri=True) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("ATTACH DATABASE ? AS live", (source.as_uri() + "?mode=ro",))
        counts = {}
        for table in TABLES:
            source_columns = {row[1] for row in conn.execute(f'PRAGMA live.table_info("{table}")')}
            columns = [row[1] for row in conn.execute(f'PRAGMA main.table_info("{table}")')
                       if row[1] in source_columns]
            if not columns:
                raise ValueError(f"Source reference table missing or incompatible: {table}")
            names = ', '.join('"' + name.replace('"', '""') + '"' for name in columns)
            values = ', '.join(
                STATION_RESETS[name] if table == "station" and name in STATION_RESETS
                else '"' + name.replace('"', '""') + '"' for name in columns
            )
            conn.execute(f'INSERT INTO main."{table}" ({names}) SELECT {values} FROM live."{table}"')
            counts[table] = conn.execute(f'SELECT count(*) FROM main."{table}"').fetchone()[0]
        conn.commit()
        conn.execute("DETACH DATABASE live")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return counts


def seed_scratch_db(live: Path, out: Path) -> dict[str, int]:
    source, target = _validate_paths(live, out)
    os.environ["DBREL_DB"] = str(target)
    print(f"DBREL_DB={target}", flush=True)
    # Import only after choosing the safe path; refuse a stale imported config.
    from dbrel import db

    if db.DB_PATH.resolve() != target:
        raise ValueError("DBREL_DB was imported earlier; run this script in a fresh process")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive reservation also closes the existence-check/create race.
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        db.create_all()
        db.dispose_engine()
        return _copy_reference_data(source, target)
    except Exception:
        db.dispose_engine()
        for path in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
            path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        counts = seed_scratch_db(args.live, args.out)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"seed_scratch_db: {exc}\n")
    for table, count in counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
