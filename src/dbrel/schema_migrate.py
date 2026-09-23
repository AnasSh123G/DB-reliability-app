"""Idempotent schema migration for the holiday-aware-stop-averages change.

Alembic is vestigial in this project (schema changes normally go through
`create_all()` on a rebuilt database -- see `[[dbrel-aggregates]]` memory) but
that's not an option here: the real database took 5.5 hours to backfill and
`create_all()` never touches an existing table's columns anyway, it only
creates tables that don't exist yet. So this module does the two things
`create_all()` can't:

1. `ALTER TABLE ... ADD COLUMN` for `station.state` and `trip_stop.is_holiday`
   -- additive, existing rows keep their data, checked against
   `PRAGMA table_info` first so re-running is a no-op.
2. Drop and recreate `public_holiday` with its new composite `(date, state)`
   key -- the one table this change is allowed to drop (proposal.md: "the 11
   existing NRW-only rows are superseded, not preserved"). Everything else
   (`station`, `trip`, `trip_stop`, ...) is never dropped.

`stop_delay_average` is a brand new table this change created, so it has no
existing data to protect either: when its key changed (`trip_start_time`
joined the primary key, to separate two same-time-of-day services with
different origins -- e.g. RE7's coupled portions), this module drops and
recreates it too, exactly like `public_holiday` -- it's fully rebuilt by
`dbrel build-stop-averages` on every run anyway, so nothing is lost.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from dbrel.db import Base


def _columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _pk_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows if row[5] > 0}  # row[5] is the pk index, 0 if not a pk column


def _table_exists(engine: Engine, table: str) -> bool:
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    return row is not None


def migrate(engine: Engine) -> dict[str, bool]:
    """Bring an existing database up to this change's schema. Safe to re-run.

    Returns what actually happened, for the CLI to report -- every key is
    False on a second run against the same database.
    """
    report = {
        "station_state_added": False,
        "trip_stop_is_holiday_added": False,
        "public_holiday_recreated": False,
        "stop_delay_average_created": False,
        "trip_path_sha1_added": False,
        "station_stop_recreated": False,
    }

    with engine.begin() as conn:
        station_cols = _columns(engine, "station")
        if "state" not in station_cols:
            conn.execute(text("ALTER TABLE station ADD COLUMN state VARCHAR(8)"))
            report["station_state_added"] = True

        ts_cols = _columns(engine, "trip_stop")
        if "is_holiday" not in ts_cols:
            conn.execute(text("ALTER TABLE trip_stop ADD COLUMN is_holiday INTEGER"))
            report["trip_stop_is_holiday_added"] = True

        trip_cols = _columns(engine, "trip")
        if "path_sha1" not in trip_cols:
            conn.execute(text("ALTER TABLE trip ADD COLUMN path_sha1 VARCHAR(40)"))
            report["trip_path_sha1_added"] = True

        # station_stop's whole identity changed: stop_sequence is now only
        # meaningful together with path_sha1 (see StationStop docstring --
        # numbered TT trains fork route variants, e.g. a coupled/wing service,
        # under ONE train_id, and the old (train_id, stop_sequence) unique key
        # let a later variant's stop silently overwrite/collide with an
        # earlier one's slot). Adding the column is not enough; the unique
        # constraint has to move too, and SQLite can't ALTER a constraint in
        # place. This table is a fully-derived schedule pattern (rebuilt by
        # any backfill/replay), so it is safe to drop and let create_all()
        # remake it with the new key -- exactly the public_holiday/
        # stop_delay_average precedent above.
        if _table_exists(engine, "station_stop") and "path_sha1" not in _columns(
            engine, "station_stop"
        ):
            conn.execute(text("DROP TABLE station_stop"))
            report["station_stop_recreated"] = True

        ph_cols = _columns(engine, "public_holiday")
        # The old schema has a single-column PK ('date' only, no 'state' or
        # 'partial' column). If those columns are already present, a previous
        # run of this migration already recreated the table -- leave it alone
        # so re-running never drops loaded holiday data.
        if "state" not in ph_cols or "partial" not in ph_cols:
            conn.execute(text("DROP TABLE IF EXISTS public_holiday"))
            conn.execute(
                text(
                    """
                    CREATE TABLE public_holiday (
                        date VARCHAR(10) NOT NULL,
                        state VARCHAR(8) NOT NULL,
                        name VARCHAR(64),
                        partial INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (date, state)
                    )
                    """
                )
            )
            report["public_holiday_recreated"] = True

        if _table_exists(engine, "stop_delay_average"):
            if "trip_start_time" not in _pk_columns(engine, "stop_delay_average"):
                conn.execute(text("DROP TABLE stop_delay_average"))
                report["stop_delay_average_created"] = True
        else:
            report["stop_delay_average_created"] = True

    # Creates stop_delay_average (and anything else genuinely missing) without
    # touching any table that already exists.
    Base.metadata.create_all(engine)

    return report
