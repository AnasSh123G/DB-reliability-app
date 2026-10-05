"""Build the deployed database from the collection database (see `dbrel.deployed`).

The collection database is opened read-only. The new file is written as `<out>.partial` and
renamed over `<out>` only once every row the server reads through the `station_stop`, `train` and
`train_variant_run` views equals the collection database's (ids included); a failed build leaves
`<out>` as it was.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import Column, Index, Integer, MetaData, Table, create_engine

from dbrel import deployed
from dbrel.db import make_engine
from dbrel.deployed import (
    BUILD_INDEXES,
    STATION_STOP_VIEW,
    TRAIN_INDEXES,
    TRAIN_LOOKUPS,
    TRAIN_VARIANT_RUN_VIEW,
    UNREAD_TRAIN_COLUMNS,
    ScheduleRow,
    pattern_digest,
    train_view,
)
from dbrel.ml.notice_rates import NoticeRates, uses_notice_rates, write_totals
from dbrel.models import Base
from dbrel.web.queries import ACTIVE_WINDOW_DAYS

# Copied whole, with the collection database's schema. stop_delay_average and
# train_stop_delay_average are empty today, but the web path queries them, so their tables must exist.
COPIED = ["feed_version", "public_holiday", "route", "service_line", "station", "passenger_line",
          "passenger_line_member", "stop_delay_average", "train_stop_delay_average"]
# Copied whole, but keeping only their named indexes: their unique constraints guard the collector's
# writes, and the server never looks rows up through them.
COPIED_LEAN = ["search_alias"]
_SCHEDULE_COLUMNS = ("id", "feed_version_id", "train_id", "path_sha1", "stop_sequence", "station_id",
                     "source_stop_id", "platform_code", "planned_arrival_sec", "planned_departure_sec",
                     "pickup_type", "drop_off_type")
_ORDER = "ORDER BY feed_version_id, train_id, path_sha1, stop_sequence"
_RUN_SUMMARY = ("SELECT train_id, path_sha1, MAX(service_date) AS last_service_date, COUNT(*) AS n_runs "
                "FROM {trip} WHERE {running} GROUP BY train_id, path_sha1")
_BATCH = 20_000


class DeployedBuildError(RuntimeError):
    """The deployed database does not reproduce the collection database."""


def _lean_copy(table: Table, metadata: MetaData) -> Table:
    """`table`'s columns and named indexes, without its constraints."""
    copy = Table(table.name, metadata, *[Column(c.name, c.type, primary_key=c.primary_key) for c in table.columns])
    for index in table.indexes:
        Index(index.name, *[copy.c[c.name] for c in index.columns], unique=index.unique)
    return copy


def _train_data(metadata: MetaData) -> tuple[Table, list[str], list[str]]:
    """The train_data table, the source train columns, and the ones train_data keeps as they are."""
    train = Base.metadata.tables["train"]
    columns = [c.name for c in train.columns]
    kept = [c for c in columns if c not in TRAIN_LOOKUPS]
    table = Table("train_data", metadata,
                  *[Column(c, train.c[c].type, primary_key=train.c[c].primary_key) for c in kept],
                  *[Column(c, Integer, nullable=False) for c in dict.fromkeys(TRAIN_LOOKUPS.values())])
    for name, cols in TRAIN_INDEXES.items():
        Index(name, *[table.c[c] for c in cols])
    return table, columns, kept


def _variants(cursor):
    """((feed, train, path_sha1), [ScheduleRow]) per variant, from rows in `_ORDER`."""
    key, rows = None, []
    for r in cursor:
        this = (r[1], r[2], r[3])
        if this != key and rows:
            yield key, rows
            rows = []
        key = this
        rows.append(ScheduleRow(r[0], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11]))
    if rows:
        yield key, rows


def _write_schedule(source: sqlite3.Connection, out: sqlite3.Connection, path_ids: dict,
                    retired: set[int]) -> dict[str, int]:
    """Stream the source schedule into patterns, variants and id runs.

    `source` is the read-only collection database, `out` the deployed one, `path_ids` maps each
    route hash (None included) to its `variant_path` id, and `retired` holds the ids of trains whose
    schedule is left out. Returns the variant, pattern and pattern-stop counts.
    """
    patterns: dict[str, int] = {}
    variants = pattern_rows = 0
    batch: dict[str, list] = {"stop": [], "variant": [], "run": []}

    def flush() -> None:
        """Write the batched rows and empty the batches."""
        out.executemany("INSERT INTO pattern_stop VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch["stop"])
        out.executemany("INSERT INTO train_variant VALUES (?, ?, ?, ?, ?, ?)", batch["variant"])
        out.executemany("INSERT INTO variant_id_run VALUES (?, ?, ?)", batch["run"])
        for rows in batch.values():
            rows.clear()

    cursor = source.execute(f"SELECT {', '.join(_SCHEDULE_COLUMNS)} FROM station_stop {_ORDER}")
    for (feed, train, path), rows in _variants(cursor):
        if train in retired:
            continue
        start, pattern, runs = deployed.encode_variant(rows)
        digest = pattern_digest(pattern)
        pattern_id = patterns.get(digest)
        if pattern_id is None:
            pattern_id = patterns[digest] = len(patterns) + 1
            batch["stop"].extend((pattern_id, position, *stop) for position, stop in enumerate(pattern))
            pattern_rows += len(pattern)
        variants += 1
        batch["variant"].append((variants, feed, train, path_ids[path], pattern_id, start))
        batch["run"].extend((variants, position, first_id) for position, first_id in runs)
        if len(batch["variant"]) >= _BATCH:
            flush()
    flush()
    out.commit()
    return {"variants": variants, "patterns": len(patterns), "pattern_stops": pattern_rows}


def _verify(label: str, columns: tuple[str, ...], key: int, theirs, ours) -> int:
    """Every row of `ours` equals the row of `theirs` at the same place; returns the row count.

    `key` is how many leading columns name a row in the error message.
    """
    n = 0
    while True:
        a, b = theirs.fetchmany(_BATCH), ours.fetchmany(_BATCH)
        for x, y in zip(a, b):
            if x != y:
                field = next(c for c, u, v in zip(columns, x, y) if u != v)
                where = ", ".join(f"{c} {v!r}" for c, v in zip(columns[:key], x[:key]))
                raise DeployedBuildError(
                    f"{label} row differs at {where}: {field} is {dict(zip(columns, x))[field]!r} in the "
                    f"collection database and {dict(zip(columns, y))[field]!r} in the deployed one")
        n += min(len(a), len(b))
        if len(a) != len(b):
            raise DeployedBuildError(f"{label} row counts differ after {n} rows: "
                                     f"the {'deployed' if len(a) > len(b) else 'collection'} database ends first")
        if not a:
            return n


def _require_station_aliases(con: sqlite3.Connection) -> None:
    """Refuse a source that has stations but no station search aliases.

    Args:
        con: connection with the collection database attached as `src`.

    Raises:
        DeployedBuildError: stations exist but `search_alias` holds no station row, so the deployed
            site could not find any station by name. `dbrel build-aliases` fills them.
    """
    stations = con.execute("SELECT COUNT(*) FROM src.station").fetchone()[0]
    aliases = con.execute("SELECT COUNT(*) FROM src.search_alias WHERE station_id IS NOT NULL").fetchone()[0]
    if stations and not aliases:
        raise DeployedBuildError(f"the collection database has {stations} stations but no station search "
                                 "aliases, so stations could not be searched by name; run `dbrel build-aliases`")


def _running(retired_ids: str) -> str:
    """SQL: the row's train_id is not among `retired_ids` (a subquery selecting train ids)."""
    return f"train_id NOT IN ({retired_ids})"


def _verify_schedule(source: sqlite3.Connection, out: sqlite3.Connection, source_retired: str) -> int:
    """Every running train's row through the station_stop view equals the source row, id included.

    `source_retired` is a subquery giving the retired train ids on `source`. Returns the row count.
    """
    # The first four columns (feed, train, variant, stop sequence) name a row in the error message.
    named = ("feed_version_id", "train_id", "path_sha1", "stop_sequence")
    columns = named + tuple(c for c in _SCHEDULE_COLUMNS if c not in named)
    select = f"SELECT {', '.join(columns)} FROM station_stop"
    return _verify("schedule", columns, 4, source.execute(f"{select} WHERE {_running(source_retired)} {_ORDER}"),
                   out.execute(f"{select} {_ORDER}"))


def build_deployed(source: Path, out: Path, *, manifest: dict | None = None,
                   log: Callable[[str], None] = print) -> dict[str, int]:
    """Build the deployed database at `out` from the collection database at `source`.

    `manifest` is the current model's: when it uses notice rates, their totals are copied after its
    fingerprint is checked. Returns the counts the build reports.
    """
    source, out = Path(source).resolve(), Path(out).resolve()
    partial = out.with_name(out.name + ".partial")
    for leftover in (partial, Path(f"{partial}-journal")):
        leftover.unlink(missing_ok=True)
    started = time.monotonic()
    try:
        engine = create_engine(f"sqlite:///{partial}")
        Base.metadata.create_all(engine, tables=[Base.metadata.tables[t] for t in COPIED])
        lean = MetaData()
        for name in COPIED_LEAN:
            _lean_copy(Base.metadata.tables[name], lean)
        _, train_columns, train_kept = _train_data(lean)
        lean.create_all(engine)
        deployed.metadata.create_all(engine)
        engine.dispose()  # the plain sqlite3 connections below write the file

        out_con = sqlite3.connect(f"file:{partial}", uri=True)  # URI filenames: the ATTACH below opens read-only
        out_con.execute("PRAGMA journal_mode=OFF")
        out_con.execute("PRAGMA synchronous=OFF")
        out_con.execute(f"ATTACH DATABASE 'file:{source}?mode=ro' AS src")
        counts: dict[str, int] = {}
        # Retired trains (the server's rule: not seen for ACTIVE_WINDOW_DAYS before the newest data)
        # keep their train row; their schedule, run summary and search aliases are left out.
        (newest,) = out_con.execute("SELECT MAX(last_seen_date) FROM src.train").fetchone()
        cutoff = (date.fromisoformat(newest) - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat() if newest else ""
        out_con.execute("CREATE TEMP TABLE retired (id INTEGER PRIMARY KEY)")
        out_con.execute("INSERT INTO retired SELECT id FROM src.train WHERE last_seen_date < ?", (cutoff,))
        retired = {train_id for (train_id,) in out_con.execute("SELECT id FROM retired")}
        counts["retired_trains"] = len(retired)
        out_retired, source_retired = "SELECT id FROM temp.retired", (
            f"SELECT id FROM train WHERE last_seen_date < '{cutoff}'")  # cutoff is an ISO date we computed
        _require_station_aliases(out_con)
        for table in COPIED + COPIED_LEAN:
            cols = ", ".join(f'"{r[1]}"' for r in out_con.execute(f"PRAGMA main.table_info({table})"))
            only_running = (f" WHERE train_id IS NULL OR {_running(out_retired)}" if table == "search_alias" else "")
            counts[table] = out_con.execute(
                f'INSERT INTO main."{table}" ({cols}) SELECT {cols} FROM src."{table}"{only_running}').rowcount
        out_con.commit()
        log(f"copied {sum(counts.values())} rows from {len(COPIED) + len(COPIED_LEAN)} tables")

        # Lookups, each distinct value once; id 0 is "none".
        lookups = {
            "variant_path": "SELECT path_sha1 AS v FROM src.train UNION SELECT path_sha1 FROM src.station_stop "
                            f"WHERE {_running(out_retired)} UNION SELECT path_sha1 FROM src.trip "
                            f"WHERE {_running(out_retired)}",
            "route_text": "SELECT route_path AS v FROM src.train",
            "place_name": "SELECT origin_name AS v FROM src.train UNION SELECT dest_name FROM src.train",
        }
        for table, values in lookups.items():
            column = BUILD_INDEXES[table]
            out_con.execute(f"INSERT INTO {table} VALUES (0, NULL)")
            counts[table] = out_con.execute(
                f"INSERT INTO {table} ({column}) SELECT DISTINCT v FROM ({values}) "
                "WHERE v IS NOT NULL ORDER BY v").rowcount
            out_con.execute(f"CREATE UNIQUE INDEX build_{table} ON {table} ({column})")
        kept = ", ".join("NULL" if c in UNREAD_TRAIN_COLUMNS else f't."{c}"' for c in train_kept)
        counts["trains"] = out_con.execute(
            f"INSERT INTO train_data ({', '.join(chr(34) + c + chr(34) for c in train_kept)}, path_id, "
            f"route_path_id, origin_name_id, dest_name_id) SELECT {kept}, COALESCE(vp.id, 0), COALESCE(rt.id, 0), "
            "COALESCE(o.id, 0), COALESCE(d.id, 0) FROM src.train t "
            "LEFT JOIN variant_path vp ON vp.path_sha1 = t.path_sha1 "
            "LEFT JOIN route_text rt ON rt.route_path = t.route_path "
            "LEFT JOIN place_name o ON o.name = t.origin_name LEFT JOIN place_name d ON d.name = t.dest_name "
            "ORDER BY t.id").rowcount
        out_con.commit()
        log(f"lookups: {counts['variant_path']} route hashes, {counts['route_text']} route paths, "
            f"{counts['place_name']} place names; {counts['trains']} trains")

        path_ids = dict(out_con.execute("SELECT path_sha1, id FROM variant_path"))
        source_con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        counts.update(_write_schedule(source_con, out_con, path_ids, retired))
        counts["train_variant_run"] = out_con.execute(
            "INSERT INTO variant_run_data SELECT g.train_id, COALESCE(vp.id, 0), g.last_service_date, g.n_runs "
            f"FROM ({_RUN_SUMMARY.format(trip='src.trip', running=_running(out_retired))}) g "
            "LEFT JOIN variant_path vp ON vp.path_sha1 = g.path_sha1 ORDER BY 1, 2").rowcount
        for view in (STATION_STOP_VIEW, TRAIN_VARIANT_RUN_VIEW, train_view(train_columns)):
            out_con.execute(view)
        out_con.commit()
        log(f"schedule: {counts['variants']} variants -> {counts['patterns']} patterns, "
            f"{counts['pattern_stops']} pattern stops; {counts['train_variant_run']} run-summary rows")

        counts["schedule_rows"] = _verify_schedule(source_con, out_con, source_retired)
        read = [c for c in train_columns if c not in UNREAD_TRAIN_COLUMNS]
        train_select = f"SELECT {', '.join(chr(34) + c + chr(34) for c in read)} FROM train ORDER BY id"
        _verify("train", tuple(read), 1, source_con.execute(train_select), out_con.execute(train_select))
        (filled,) = out_con.execute("SELECT COUNT(*) FROM train WHERE " + " OR ".join(
            f'"{c}" IS NOT NULL' for c in UNREAD_TRAIN_COLUMNS)).fetchone()
        if filled:
            raise DeployedBuildError(f"{filled} trains have a value in a column the server never reads")
        summary = ("train_id", "path_sha1", "last_service_date", "n_runs")
        running_summary = _RUN_SUMMARY.format(trip="trip", running=_running(source_retired))
        _verify("run summary", summary, 2, source_con.execute(f"{running_summary} ORDER BY train_id, path_sha1"),
                out_con.execute(f"SELECT {', '.join(summary)} FROM train_variant_run ORDER BY train_id, path_sha1"))
        log(f"verified {counts['schedule_rows']} schedule rows of running trains, {counts['trains']} trains and "
            f"{counts['train_variant_run']} run-summary rows against the collection database "
            f"({counts['retired_trains']} retired trains keep only their train row)")
        for table in BUILD_INDEXES:  # the server only joins the lookups by id
            out_con.execute(f"DROP INDEX build_{table}")
        source_con.close()
        out_con.execute("DETACH DATABASE src")
        out_con.close()

        if uses_notice_rates(manifest or {}):
            record = manifest["notice_rates"]
            rates = NoticeRates.from_record(make_engine(source), record)  # refuses if the counts changed
            target = create_engine(f"sqlite:///{partial}")
            counts["notice_rate_total"] = write_totals(target, rates)
            target.dispose()
            log(f"notice-rate totals: {counts['notice_rate_total']} rows (fingerprint {record['fingerprint'][:12]} "
                "matches the model)")

        finish = sqlite3.connect(partial)
        # Statistics let SQLite start a board from the station's pattern stops instead of scanning
        # every variant of the feed (2026-10-05: 0.84 s -> 0.65 s for Köln Hbf, no size cost).
        finish.execute("ANALYZE")
        finish.execute("VACUUM")
        finish.close()
        os.replace(partial, out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    size = out.stat().st_size
    counts["size_bytes"] = size
    log(f"{out}: {size / 2**20:.0f} MiB, {counts['schedule_rows']} schedule rows from {counts['patterns']} "
        f"patterns ({counts['pattern_stops']} pattern stops), {counts['train_variant_run']} run-summary rows, "
        f"{time.monotonic() - started:.0f}s")
    return counts
