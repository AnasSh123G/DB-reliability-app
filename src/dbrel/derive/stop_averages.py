"""Build stop_delay_average: mean arrival/departure delay per station, line,
direction, trip start time, scheduled time-of-day, and holiday flag.

Full rebuild every run (DELETE + INSERT in one transaction) -- see
stop-delay-averages spec, "precomputed, not scored live". Direction comes
from `dbrel.direction` (the same heuristic the web UI uses, not a second
one); everything else is set-based SQL over `trip_stop`, because it is
14.9M+ rows and the project's constraint on this change is explicit that bulk
work over it must not loop row by row in Python.

`trip_start_time` (part of the key -- see class docstring on
`StopDelayAverage`): the run's PLANNED departure at its own origin, Berlin-
local 'HH:MM', derived PER TRIP, not per train. `train.first_departure_sec`
(the train pattern's static first-tracked-departure) can't do this job: the
same numbered service can run at different times on different days, and
worse, two entirely different trips (e.g. RE7's coupled portions -- one
originating in Flensburg, one in Kiel, joined into a single physical train
partway) can share a scheduled time at a shared downstream station while
having different origin times -- pooling them under one key would average
together two services a passenger would never consider "the same train".

The source: `trip.source_run_key` for a Timetables-sourced (`source='tts'`)
trip is built as `f"{daily_trip_id}-{start_stamp}"` (see ingest/runs.py and
parser.py's `TtStop.run_key`/`split_stop_id`), where `start_stamp` is always
exactly the last 10 characters and is a literal `yyMMddHHmm` Berlin wall-
clock timestamp of the run's origin departure (`parser.py`'s `parse_ts`
docstring) -- not UTC, so no DST conversion is needed, unlike `sched_time`.
`daily_trip_id` is a signed integer and never itself contains a hyphen, so
the trailing 10 characters of `source_run_key` are unambiguously the stamp
regardless of its own sign. HH is characters -4:-2 of that stamp, MM is -2:.
For a GTFS-sourced trip (`source='gtfs'`) there is no such per-trip run id --
GTFS-de trips are pattern-based, so `train.first_departure_sec` (its static
scheduled origin departure, in GTFS seconds) is used as a fallback, converted
to 'HH:MM' by `% 86400`. If neither yields a usable value the trip is
excluded from averaging entirely (reported, not silently dropped) --
`build_stop_averages`'s return dict carries the count.
"""

from __future__ import annotations

import time as _time

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dbrel.derive.berlin_time_sql import local_hhmm_case_sql
from dbrel.direction import direction_by_train_id
from dbrel.models import ServiceLine

# Guards against a malformed/short source_run_key ever producing a bogus
# "trip start time" like '99:99' -- glob patterns constrain each half to two
# digits, and the surrounding int-range check constrains them to a real
# clock time.

# A raw |delay| beyond this, on a stop that already passed the
# RECORDED/not-cancelled filter, is overwhelmingly a data-matching defect
# rather than a real observation -- e.g. rail-replacement-bus lines whose
# actual times get compared against the original train line's schedule,
# yielding readings as extreme as exactly -86400s (a day-boundary bug). Real
# delays here average ~2-3 minutes, so 2 hours is a generous plausibility
# cap. Evaluated independently per side (arrival vs. departure), matching
# this file's existing arrival/departure independence: an extreme arrival
# delay does not disqualify that stop's departure delay from n_dep, and vice
# versa. See docs/probe-findings.md (S9/S46 audit, 2026-09-23).
MAX_PLAUSIBLE_DELAY_S = 7200

_TRIP_START_TTS_SQL = """
    CASE WHEN t.source = 'tts'
              AND length(t.source_run_key) >= 10
              AND substr(t.source_run_key, -4, 2) GLOB '[0-9][0-9]'
              AND substr(t.source_run_key, -2, 2) GLOB '[0-9][0-9]'
              AND CAST(substr(t.source_run_key, -4, 2) AS INTEGER) BETWEEN 0 AND 23
              AND CAST(substr(t.source_run_key, -2, 2) AS INTEGER) BETWEEN 0 AND 59
         THEN substr(t.source_run_key, -4, 2) || ':' || substr(t.source_run_key, -2, 2)
         WHEN t.source = 'gtfs' AND tr.first_departure_sec IS NOT NULL
         THEN printf('%02d:%02d', (tr.first_departure_sec / 3600) % 24,
                     (tr.first_departure_sec / 60) % 60)
         ELSE NULL
    END
"""


def _service_date_years(engine: Engine) -> list[int]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT DISTINCT service_date FROM trip").fetchall()
    years = {int(r[0][:4]) for r in rows if r[0]}
    return sorted(years)


def _build_direction_temp_table(engine: Engine, session: Session) -> None:
    """train_id -> direction (0/1), for every line that has any trains at all.

    Python loop over lines (~780) and, within each, over that line's trains
    (73k total across all lines) -- both within the "fine to loop" carve-out
    of this change's constraints; only trip_stop-scale work has to be SQL.
    """
    line_ids = session.execute(select(ServiceLine.id)).scalars().all()
    rows: list[tuple[int, int]] = []
    for line_id in line_ids:
        for train_id, direction in direction_by_train_id(session, line_id).items():
            rows.append((train_id, direction))

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS train_direction")
        conn.exec_driver_sql(
            "CREATE TEMP TABLE train_direction (train_id INTEGER PRIMARY KEY, direction INTEGER)"
        )
        conn.exec_driver_sql("CREATE INDEX idx_train_direction_dir ON train_direction(direction)")
        if rows:
            conn.execute(
                text("INSERT INTO train_direction (train_id, direction) VALUES (:t, :d)"),
                [{"t": t, "d": d} for t, d in rows],
            )


def build_stop_averages(engine: Engine, session: Session) -> dict:
    """(Re)build the whole stop_delay_average table. Re-runnable, deterministic.

    Returns a small report dict: row counts in, rows out, elapsed seconds, and
    how many otherwise-eligible trip_stop rows were excluded because their
    trip's start time couldn't be derived (see module docstring).
    """
    t0 = _time.time()
    years = _service_date_years(engine)
    _build_direction_temp_table(engine, session)

    sched_time_sql = local_hhmm_case_sql(
        "COALESCE(ts.supposed_departure_utc, ts.supposed_arrival_utc)", years
    )

    built_at = int(_time.time())

    with engine.begin() as conn:
        excluded_no_start_time = conn.exec_driver_sql(
            f"""
            SELECT COUNT(*)
            FROM trip_stop ts
            JOIN trip t ON t.id = ts.trip_id
            JOIN train tr ON tr.id = t.train_id
            JOIN train_direction td ON td.train_id = tr.id
            WHERE ts.quality = 'RECORDED'
              AND ts.cancelled = 0
              AND ts.is_holiday IS NOT NULL
              AND tr.line_id IS NOT NULL
              AND ({_TRIP_START_TTS_SQL}) IS NULL
            """
        ).fetchone()[0]

        conn.exec_driver_sql("DELETE FROM stop_delay_average")
        conn.exec_driver_sql(
            f"""
            INSERT INTO stop_delay_average (
                station_id, line_id, direction, trip_start_time, sched_time, is_holiday,
                n_arr, mean_arr_delay_s, n_dep, mean_dep_delay_s, built_at
            )
            SELECT
                ts.station_id,
                tr.line_id,
                td.direction,
                {_TRIP_START_TTS_SQL} AS trip_start_time,
                {sched_time_sql} AS sched_time,
                ts.is_holiday,
                SUM(CASE WHEN ts.arr_delay_s IS NOT NULL AND ABS(ts.arr_delay_s) <= {MAX_PLAUSIBLE_DELAY_S} THEN 1 ELSE 0 END) AS n_arr,
                AVG(CASE WHEN ts.arr_delay_s IS NOT NULL AND ABS(ts.arr_delay_s) <= {MAX_PLAUSIBLE_DELAY_S} THEN ts.arr_delay_s END) AS mean_arr_delay_s,
                SUM(CASE WHEN ts.dep_delay_s IS NOT NULL AND ABS(ts.dep_delay_s) <= {MAX_PLAUSIBLE_DELAY_S} THEN 1 ELSE 0 END) AS n_dep,
                AVG(CASE WHEN ts.dep_delay_s IS NOT NULL AND ABS(ts.dep_delay_s) <= {MAX_PLAUSIBLE_DELAY_S} THEN ts.dep_delay_s END) AS mean_dep_delay_s,
                {built_at} AS built_at
            FROM trip_stop ts
            JOIN trip t ON t.id = ts.trip_id
            JOIN train tr ON tr.id = t.train_id
            JOIN train_direction td ON td.train_id = tr.id
            WHERE ts.quality = 'RECORDED'
              AND ts.cancelled = 0
              AND ts.is_holiday IS NOT NULL
              AND tr.line_id IS NOT NULL
              AND ({_TRIP_START_TTS_SQL}) IS NOT NULL
            GROUP BY ts.station_id, tr.line_id, td.direction, trip_start_time, sched_time, ts.is_holiday
            """
        )
        n_rows = conn.exec_driver_sql("SELECT COUNT(*) FROM stop_delay_average").fetchone()[0]
        conn.exec_driver_sql("DROP TABLE IF EXISTS train_direction")

    return {
        "years": years,
        "rows": n_rows,
        "excluded_no_start_time": excluded_no_start_time,
        "elapsed_s": _time.time() - t0,
    }
