"""The deployed database: the read-only copy of the collection database a deployed server reads.

`scripts/build_slim_db.py` builds it (`dbrel.deployed_build`). Its tables are defined here, on
their own `MetaData`, so `Base.metadata.create_all()` never creates them in the collection
database: the server tells the two databases apart by whether `train_variant_run` exists. In a
deployed database, `station_stop`, `train` and `train_variant_run` are views that give back the
collection database's rows exactly.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import Column, Index, Integer, MetaData, String, Table, Text

metadata = MetaData()  # the tables the build creates
view_metadata = MetaData()  # the columns of its views, for reading them with SQLAlchemy Core

# Id 0 in each lookup table stands for "none" (no route hash, path or name), so every view joins
# with plain inner joins, which SQLite merges into the outer query.

# Each distinct value once. The server only joins these by id; the build adds and later drops the
# indexes it needs to look values up (BUILD_INDEXES).
VARIANT_PATH = Table(  # route hashes (path_sha1) from trains, schedule variants and runs
    "variant_path",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("path_sha1", String(40)),
)
ROUTE_TEXT = Table(  # trains' route_path station lists
    "route_text",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("route_path", Text),
)
PLACE_NAME = Table(  # trains' origin and destination names
    "place_name",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text),
)
BUILD_INDEXES = {"variant_path": "path_sha1", "route_text": "route_path", "place_name": "name"}

# One row per train and route variant (path 0 = runs without a hash) over every recorded run: what
# the server reads `trip` for. A train has at most one run per service date.
VARIANT_RUN_DATA = Table(
    "variant_run_data",
    metadata,
    Column("train_id", Integer, primary_key=True),
    Column("path_id", Integer, primary_key=True),
    Column("last_service_date", String(10), nullable=False),
    Column("n_runs", Integer, nullable=False),
    sqlite_with_rowid=False,
)
# ... read through this view, which has the columns the server's helper selects.
TRAIN_VARIANT_RUN = Table(
    "train_variant_run",
    view_metadata,
    Column("train_id", Integer),
    Column("path_sha1", String(40)),
    Column("last_service_date", String(10)),
    Column("n_runs", Integer),
)
TRAIN_VARIANT_RUN_VIEW = """
CREATE VIEW train_variant_run AS
SELECT r.train_id AS train_id, vp.path_sha1 AS path_sha1, r.last_service_date AS last_service_date,
       r.n_runs AS n_runs
FROM variant_run_data r JOIN variant_path vp ON vp.id = r.path_id
"""

# The schedule (see "shared stop patterns" below). `station_stop` is a view over these.
PATTERN_STOP = Table(
    "pattern_stop",
    metadata,
    Column("pattern_id", Integer, primary_key=True),
    Column("position", Integer, primary_key=True),  # 0.. in stop-sequence order
    Column("stop_sequence", Integer, nullable=False),
    Column("station_id", Integer, nullable=False),
    Column("source_stop_id", String(64)),
    Column("platform_code", String(32)),
    Column("arr_offset", Integer),  # seconds after the variant's start_sec
    Column("dep_offset", Integer),
    Column("pickup_type", Integer),
    Column("drop_off_type", Integer),
    Index("ix_pattern_stop_station", "station_id", "pattern_id"),
    sqlite_with_rowid=False,
)
TRAIN_VARIANT = Table(
    "train_variant",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("feed_version_id", Integer, nullable=False),
    Column("train_id", Integer, nullable=False),
    Column("path_id", Integer, nullable=False),
    Column("pattern_id", Integer, nullable=False),
    Column("start_sec", Integer),  # the variant's earliest scheduled time; NULL when it has none
    Index("ix_train_variant_key", "train_id", "feed_version_id", "path_id", unique=True),
    Index("ix_train_variant_pattern", "pattern_id", "feed_version_id"),
)
VARIANT_ID_RUN = Table(
    "variant_id_run",
    metadata,
    Column("variant_id", Integer, primary_key=True),
    Column("from_position", Integer, primary_key=True),
    Column("first_id", Integer, nullable=False),  # the row id at from_position; +1 per position after
    sqlite_with_rowid=False,
)

# Exactly the columns of the collection database's station_stop table, row ids included.
STATION_STOP_VIEW = """
CREATE VIEW station_stop AS
SELECT
    (SELECT r.first_id + p.position - r.from_position FROM variant_id_run r
      WHERE r.variant_id = v.id AND r.from_position <= p.position
      ORDER BY r.from_position DESC LIMIT 1) AS id,
    v.feed_version_id AS feed_version_id,
    v.train_id AS train_id,
    p.stop_sequence AS stop_sequence,
    p.station_id AS station_id,
    p.source_stop_id AS source_stop_id,
    p.platform_code AS platform_code,
    v.start_sec + p.arr_offset AS planned_arrival_sec,
    v.start_sec + p.dep_offset AS planned_departure_sec,
    p.pickup_type AS pickup_type,
    p.drop_off_type AS drop_off_type,
    vp.path_sha1 AS path_sha1
FROM train_variant v
JOIN pattern_stop p ON p.pattern_id = v.pattern_id
JOIN variant_path vp ON vp.id = v.path_id
"""

# `train` in a deployed database: train_data holds every column of the collection database's train
# except these, which it stores as ids into the lookup tables above.
TRAIN_LOOKUPS = {"path_sha1": "path_id", "route_path": "route_path_id", "origin_name": "origin_name_id",
                 "dest_name": "dest_name_id"}
# Train columns nothing in the server, the model code or the frontend reads (checked 2026-10-05):
# a deployed database leaves them empty.
UNREAD_TRAIN_COLUMNS = ("dest_station_id", "first_seen_date", "first_seen_feed_version_id", "identity_kind",
                        "last_arrival_sec", "last_seen_feed_version_id", "long_name", "n_runs_seen",
                        "n_stops_tracked", "passenger_label", "route_id", "train_number_raw", "trip_type")
# The train indexes the server filters by (id is the primary key); the collector's others are left out.
TRAIN_INDEXES = {"ix_train_data_line": ("line_id", "train_number"),
                 "ix_train_data_category": ("category", "train_number")}


def train_view(columns: list[str]) -> str:
    """CREATE VIEW train giving back `columns` (the collection database's, in its order)."""
    source = {"path_sha1": "vp.path_sha1", "route_path": "rt.route_path", "origin_name": "o.name",
              "dest_name": "d.name"}
    select = ",\n    ".join(f'{source.get(c, "t." + chr(34) + c + chr(34))} AS "{c}"' for c in columns)
    return f"""
CREATE VIEW train AS
SELECT
    {select}
FROM train_data t
JOIN variant_path vp ON vp.id = t.path_id
JOIN route_text rt ON rt.id = t.route_path_id
JOIN place_name o ON o.id = t.origin_name_id
JOIN place_name d ON d.id = t.dest_name_id
"""


# -- the schedule as shared stop patterns -----------------------------------------------------------
#
# A route variant's stops (station_stop rows of one feed, train and path_sha1, in stop-sequence
# order) become: its start time (the earliest scheduled time), a pattern (every per-stop field,
# times relative to the start) shared by every variant with the same one, and its rows' ids as runs
# of consecutive ids. `decode_variant` gives back the rows exactly.

PATTERN_FIELDS = ("stop_sequence", "station_id", "source_stop_id", "platform_code", "arr_offset",
                  "dep_offset", "pickup_type", "drop_off_type")


@dataclass(frozen=True)
class ScheduleRow:
    """One station_stop row, without the fields its variant holds (feed, train, path_sha1)."""

    id: int
    stop_sequence: int
    station_id: int
    source_stop_id: str | None
    platform_code: str | None
    planned_arrival_sec: int | None
    planned_departure_sec: int | None
    pickup_type: int | None
    drop_off_type: int | None


Pattern = tuple[tuple, ...]  # one PATTERN_FIELDS tuple per stop


def _shift(value: int | None, by: int | None) -> int | None:
    """`value` minus `by` (0 when `by` is None); None stays None."""
    return None if value is None else value - (by or 0)


def encode_variant(rows: list[ScheduleRow]) -> tuple[int | None, Pattern, list[tuple[int, int]]]:
    """(start time, pattern, id runs) of one variant's rows, given in stop-sequence order.

    The start is the earliest of each stop's departure-or-arrival time, None when no stop has a
    time. An id run is (first position, first id) for each stretch of consecutive ids.
    """
    times = [r.planned_departure_sec if r.planned_departure_sec is not None else r.planned_arrival_sec
             for r in rows]
    known = [t for t in times if t is not None]
    start = min(known) if known else None
    pattern = tuple(
        (r.stop_sequence, r.station_id, r.source_stop_id, r.platform_code,
         _shift(r.planned_arrival_sec, start), _shift(r.planned_departure_sec, start),
         r.pickup_type, r.drop_off_type)
        for r in rows)
    runs: list[tuple[int, int]] = []
    for position, row in enumerate(rows):
        if position == 0 or row.id != rows[position - 1].id + 1:
            runs.append((position, row.id))
    return start, pattern, runs


def decode_variant(start: int | None, pattern: Pattern, runs: list[tuple[int, int]]) -> list[ScheduleRow]:
    """The rows `encode_variant` was given."""
    out = []
    starts = [p for p, _ in runs]
    for position, (seq, station, source_stop, platform, arr, dep, pickup, drop_off) in enumerate(pattern):
        from_position, first_id = runs[bisect.bisect_right(starts, position) - 1]
        out.append(ScheduleRow(
            id=first_id + position - from_position, stop_sequence=seq, station_id=station,
            source_stop_id=source_stop, platform_code=platform,
            planned_arrival_sec=None if arr is None else arr + (start or 0),
            planned_departure_sec=None if dep is None else dep + (start or 0),
            pickup_type=pickup, drop_off_type=drop_off))
    return out


def pattern_digest(pattern: Pattern) -> str:
    """A stable key for a pattern: the build looks patterns up by it."""
    return hashlib.sha1(json.dumps(pattern, separators=(",", ":")).encode()).hexdigest()
