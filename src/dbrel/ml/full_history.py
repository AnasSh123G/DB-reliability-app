"""The full-history arrival dataset: one shard per calendar month of memory-mapped columns.

Built for a year of national data (~130 M arrivals), where the single-file
`arrival_dataset.npz` no longer fits in memory. It has the same rows and filters
as `dataset.build_arrival_dataset`, read one service-date month at a time and
written straight to disk chunk by chunk, so the build's memory does not grow
with the months.

Layout under `data/ml/arrival_full/`:

    manifest.json          per shard: rows, dates, rows per date, month fingerprint
    dictionary.json        text values -> int16 codes per text column; append-only
    directions.json        train id -> direction, re-resolved on every build
    <YYYY-MM>/<column>.bin raw little-endian arrays, dtypes in COLUMNS

Shards hold raw inputs, not features. The numeric features are computed when
read (`features.numeric_features`) and the categoricals encoded against
whatever vocabulary the caller fixed, so a feature change needs no rebuild.

A rebuild redoes the latest month, every month whose fingerprint (row count,
id and finalisation high-water marks, delay/holiday/observation sums) changed,
and everything after a code-version change or with `full=True`. Station,
train and route-rank tables are read as they are now; after a change to those,
rebuild with `full=True`. Each shard is written beside the old one and swapped
in; the manifest is written last and describes only shards on disk.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterator
from pathlib import Path

import numpy as np

from dbrel.derive.berlin_time_sql import offset_breakpoints
from dbrel.ml.checkpoint import _write_json
from dbrel.ml.dataset import (
    _RANK_TABLE_SQL, MONTH_FROM, MONTH_RANGE, Dataset, _hhmm_to_sec, _local_sec_of_day, _query, _where,
    fingerprint,
)
from dbrel.ml.features import (
    CATEGORICAL, NO_LINE, NUMERIC_SOURCES, UNKNOWN, UNKNOWN_DIRECTION, day_of_year_indices, encode_column,
    numeric_features,
)

CODE_VERSION = 1
MANIFEST_NAME = "manifest.json"
DICTIONARY_NAME = "dictionary.json"
DIRECTIONS_NAME = "directions.json"
CUTOFF_HOURS = 5
_CHUNK = 250_000
READ_CHUNK = 1_000_000

COLUMNS = {
    "day": "<i4",  # days since 1970-01-01 of the service date
    "station_id": "<i4",
    "train_id": "<i4",
    "line_id": "<i4",  # NO_LINE for a train without one
    "category": "<i2",  # codes into dictionary.json
    "operator": "<i2",
    "state": "<i2",
    "segment": "<i2",
    "is_holiday": "<i1",
    "sched_local_sec": "<i4",
    "trip_start_sec": "<f4",  # NaN if unknown
    "stop_rank": "<f4",
    "n_stops": "<f4",
    "lat": "<f4",
    "lon": "<f4",
    "arr_min": "<f4",
    "stop_id": "<i8",
    "trip_id": "<i8",
    "stop_sequence": "<i4",
    "scheduled_arrival_utc": "<i8",
    "last_observed_at": "<i8",  # -1 if unknown
    "finalized_at": "<i8",  # -1 if unknown
    "v2_sched_local_sec": "<i4",
}
TEXT_COLUMNS = ("category", "operator", "state", "segment")
# Categorical feature -> the shard column it is read from (weekday and direction are derived).
_SOURCE = {"station": "station_id", "train": "train_id", "line": "line_id",
           "category": "category", "operator": "operator", "state": "state"}

_MONTH_QUERY = _query(arrival_only=True, by_date_range=True)
_MONTH_FINGERPRINT_SQL = f"""
    SELECT COUNT(*), COALESCE(MAX(ts.id), 0), COALESCE(MAX(t.finalized_at), 0),
           COALESCE(SUM(ts.arr_delay_s), 0), COALESCE(SUM(ts.is_holiday), 0),
           COALESCE(SUM(ts.last_observed_at), 0)
    {MONTH_FROM}
    {_where(arrival_only=True)}""" + MONTH_RANGE


_MONTH_DIR = re.compile(r"\d{4}-\d{2}")


class StaleHistory(RuntimeError):
    """The files on disk do not match the manifest; rebuild with `dbrel ml build-full-dataset`."""


def month_bounds(month: str) -> tuple[str, str]:
    """'2025-12' -> ('2025-12-01', '2026-01-01'), the half-open service-date range."""
    y, m = (int(x) for x in month.split("-"))
    return f"{month}-01", f"{y + m // 12:04d}-{m % 12 + 1:02d}-01"


def epoch_days(dates: Collection[str]) -> np.ndarray:
    return np.array(sorted(dates), dtype="datetime64[D]").astype(np.int64)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    # A year of station_stop makes the rank table too big to keep in RAM under the job cap.
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-262144")
    return conn


def _read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _codes(values, known: list[str]) -> np.ndarray:
    """Text -> int16 codes, appending unseen values to `known` in place."""
    values = ["" if v is None else str(v) for v in values]
    index = {v: i for i, v in enumerate(known)}
    for v in dict.fromkeys(values):
        if v not in index:
            index[v] = len(known)
            known.append(v)
    if len(known) > np.iinfo(np.int16).max:
        raise ValueError("more distinct text values than an int16 code can hold")
    return np.fromiter((index[v] for v in values), dtype=np.int16, count=len(values))


def _nan_if_none(values) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in values], dtype=np.float64)


def _shard_columns(rows: list[tuple], dictionary: dict[str, list[str]],
                   breakpoints: list[tuple[int, int]]) -> dict[str, np.ndarray]:
    (stop_id, stop_sequence, scheduled_arrival_utc, last_observed_at, finalized_at, v2_sched_utc,
     trip_id, service_date, station_id, train_id, line_id, category, operator, segment,
     state, lat, lon, holiday, sched_utc, trip_start, stop_rank, n_stops, arr_s, _dep_s) = zip(*rows)
    return {
        "day": np.array(service_date, dtype="datetime64[D]").astype(np.int64),
        "station_id": np.array(station_id, dtype=np.int64),
        "train_id": np.array(train_id, dtype=np.int64),
        "line_id": np.array([NO_LINE if v is None else v for v in line_id], dtype=np.int64),
        "category": _codes(category, dictionary["category"]),
        "operator": _codes(operator, dictionary["operator"]),
        "state": _codes(state, dictionary["state"]),
        "segment": _codes(segment, dictionary["segment"]),
        "is_holiday": np.array(holiday, dtype=np.int8),
        "sched_local_sec": _local_sec_of_day(np.array(sched_utc, dtype=np.int64), breakpoints),
        "trip_start_sec": _hhmm_to_sec(trip_start),
        "stop_rank": _nan_if_none(stop_rank),
        "n_stops": _nan_if_none(n_stops),
        "lat": np.array(lat, dtype=np.float64),
        "lon": np.array(lon, dtype=np.float64),
        "arr_min": (np.array(arr_s, dtype=np.float64) / 60).astype(np.float32),
        "stop_id": np.array(stop_id, dtype=np.int64),
        "trip_id": np.array(trip_id, dtype=np.int64),
        "stop_sequence": np.array(stop_sequence, dtype=np.int64),
        "scheduled_arrival_utc": np.array(scheduled_arrival_utc, dtype=np.int64),
        "last_observed_at": np.array([v or -1 for v in last_observed_at], dtype=np.int64),
        "finalized_at": np.array([v or -1 for v in finalized_at], dtype=np.int64),
        "v2_sched_local_sec": _local_sec_of_day(np.array(v2_sched_utc, dtype=np.int64), breakpoints),
    }


def _build_shard(conn, root: Path, month: str, dictionary: dict, breakpoints, progress) -> dict:
    started = time.monotonic()
    partial = root / f".{month}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir()
    per_day: Counter = Counter()
    n = 0
    files = {name: (partial / f"{name}.bin").open("wb") for name in COLUMNS}
    try:
        cursor = conn.execute(_MONTH_QUERY, month_bounds(month))
        while rows := cursor.fetchmany(_CHUNK):
            cols = _shard_columns(rows, dictionary, breakpoints)
            for name, fh in files.items():
                fh.write(np.ascontiguousarray(cols[name], dtype=COLUMNS[name]).tobytes())
            days, counts = np.unique(cols["day"], return_counts=True)
            per_day.update(dict(zip(days.tolist(), counts.tolist())))
            n += len(rows)
            if progress:
                progress({"month": month, "rows": n, "done": False})
    finally:
        for fh in files.values():
            fh.close()
    # Codes written into the shard must be resolvable before the shard is visible.
    _write_json(root / DICTIONARY_NAME, dictionary)
    final, old = root / month, root / f".{month}.old"
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        final.rename(old)
    partial.rename(final)
    shutil.rmtree(old, ignore_errors=True)
    rows_per_date = {str(np.datetime64(d, "D")): c for d, c in sorted(per_day.items())}
    if progress:
        progress({"month": month, "rows": n, "done": True})
    return {"rows": n, "dates": list(rows_per_date), "rows_per_date": rows_per_date,
            "built_at": time.time(), "build_seconds": round(time.monotonic() - started, 1)}


def build_full_history(db_path: Path, root: Path, directions: dict[int, int], *, full: bool = False,
                       progress: Callable[[dict], None] | None = None) -> dict:
    """Build or refresh the monthly shards under `root`; returns the manifest.

    `directions` is `features.all_directions(session)`, as for `build_dataset`.
    `progress` gets {"month", "rows", "done"} after every chunk and each month.
    """
    started = time.monotonic()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    old = _read_json(root / MANIFEST_NAME, None)
    reuse = {} if full or old is None or old.get("code_version") != CODE_VERSION else old["shards"]
    dictionary = _read_json(root / DICTIONARY_NAME, {name: [] for name in TEXT_COLUMNS})
    conn = _connect_readonly(db_path)
    try:
        months = [m for (m,) in conn.execute(
            "SELECT DISTINCT substr(service_date, 1, 7) FROM trip ORDER BY 1")]
        prints = {m: list(conn.execute(_MONTH_FINGERPRINT_SQL, month_bounds(m)).fetchone())
                  for m in months}
        months = [m for m in months if prints[m][0] > 0]
        todo = [m for m in months if m == months[-1] or m not in reuse
                or reuse[m]["fingerprint"] != prints[m] or not (root / m).is_dir()]
        shards = {m: reuse[m] for m in months if m not in todo}
        if todo:
            breakpoints = offset_breakpoints(sorted({int(m[:4]) for m in months}))
            conn.execute("DROP TABLE IF EXISTS temp.ml_stop_rank")
            conn.execute(_RANK_TABLE_SQL)
            conn.execute("CREATE UNIQUE INDEX temp.ml_stop_rank_id ON ml_stop_rank(id)")
            for m in todo:
                shards[m] = {**_build_shard(conn, root, m, dictionary, breakpoints, progress),
                             "fingerprint": prints[m]}
    finally:
        conn.close()
    for stale in root.iterdir():
        if stale.is_dir() and _MONTH_DIR.fullmatch(stale.name) and stale.name not in shards:
            shutil.rmtree(stale)
    _write_json(root / DIRECTIONS_NAME, {str(k): v for k, v in sorted(directions.items())})
    manifest = {
        "kind": "arrival-full",
        "code_version": CODE_VERSION,
        "db_path": str(db_path),
        "db_fingerprint": fingerprint(db_path),
        "built_at": time.time(),
        "build_seconds": round(time.monotonic() - started, 1),
        "rebuilt": todo,
        "cutoff_hours": CUTOFF_HOURS,
        "timetable_asof_verified": False,
        "columns": COLUMNS,
        "n_rows": sum(s["rows"] for s in shards.values()),
        "shards": {m: shards[m] for m in sorted(shards)},
    }
    _write_json(root / MANIFEST_NAME, manifest)
    return manifest


class FullHistory:
    """Read side: memory-mapped shards, streamed in chunks for chosen dates."""

    def __init__(self, root: Path):
        self.root = Path(root)
        path = self.root / MANIFEST_NAME
        if not path.exists():
            raise FileNotFoundError(f"no full-history dataset in {self.root} -- run "
                                    "`dbrel ml build-full-dataset` first")
        self.manifest = json.loads(path.read_text())
        if self.manifest.get("code_version") != CODE_VERSION:
            raise StaleHistory(f"{self.root} was built by another code version -- rebuild it")
        self.dictionary = json.loads((self.root / DICTIONARY_NAME).read_text())
        directions = json.loads((self.root / DIRECTIONS_NAME).read_text())
        ids = np.array(sorted(int(k) for k in directions), dtype=np.int64)
        self._dir_ids = ids
        self._dir_values = np.array([directions[str(i)] for i in ids.tolist()], dtype=np.int64)

    @property
    def rows_per_date(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for shard in self.manifest["shards"].values():
            out.update(shard["rows_per_date"])
        return dict(sorted(out.items()))

    @property
    def dates(self) -> list[str]:
        return list(self.rows_per_date)

    def open_shard(self, month: str, names: Collection[str] = COLUMNS) -> dict[str, np.ndarray]:
        rows = self.manifest["shards"][month]["rows"]
        out = {}
        for name in names:
            path = self.root / month / f"{name}.bin"
            dtype = np.dtype(COLUMNS[name])
            if not path.exists() or path.stat().st_size != rows * dtype.itemsize:
                raise StaleHistory(f"{path} does not hold the {rows} rows the manifest lists -- rebuild")
            out[name] = np.memmap(path, dtype=dtype, mode="r", shape=(rows,)) if rows else \
                np.zeros(0, dtype=dtype)
        return out

    def months_for(self, dates: Collection[str] | None) -> list[str]:
        if dates is None:
            return list(self.manifest["shards"])
        wanted = {d[:7] for d in dates}
        return [m for m in self.manifest["shards"] if m in wanted]

    def chunks(self, dates: Collection[str] | None = None, names: Collection[str] = COLUMNS,
               chunk_rows: int = READ_CHUNK) -> Iterator[dict[str, np.ndarray]]:
        """In-memory column chunks of the rows on `dates` (all dates if None), shard by shard.

        Shards without a wanted date are never opened.
        """
        names = list(dict.fromkeys(["day", *names]))
        wanted = None if dates is None else epoch_days(dates)
        for month in self.months_for(dates):
            cols = self.open_shard(month, names)
            n = len(cols["day"])
            for s in range(0, n, chunk_rows):
                day = np.asarray(cols["day"][s:s + chunk_rows])
                keep = slice(None) if wanted is None else np.isin(day, wanted)
                if wanted is not None and not keep.any():
                    continue
                yield {name: np.asarray(col[s:s + chunk_rows])[keep] for name, col in cols.items()}

    def direction_of(self, train_id: np.ndarray) -> np.ndarray:
        if not len(self._dir_ids):
            return np.full(len(train_id), UNKNOWN_DIRECTION, dtype=np.int64)
        pos = np.minimum(np.searchsorted(self._dir_ids, train_id), len(self._dir_ids) - 1)
        return np.where(self._dir_ids[pos] == train_id, self._dir_values[pos], UNKNOWN_DIRECTION)

    def categorical_values(self, chunk: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Raw categorical values as `compute_features` gives them; text columns stay int16 codes."""
        out = {feature: chunk[source] for feature, source in _SOURCE.items()}
        out["weekday"] = (chunk["day"].astype(np.int64) + 3) % 7  # 1970-01-01 was a Thursday
        out["direction"] = self.direction_of(chunk["train_id"].astype(np.int64))
        return out

    def text(self, name: str, codes: np.ndarray) -> np.ndarray:
        return np.array(self.dictionary[name] or [""], dtype=str)[codes]

    @staticmethod
    def numeric(chunk: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The chunk's `ARRIVAL_NUMERIC` feature columns."""
        raw = {name: chunk[name] for name in NUMERIC_SOURCES if name != "day_of_year_idx"}
        raw["day_of_year_idx"] = day_of_year_indices(chunk["day"])
        return numeric_features(raw)

    def text_values(self, name: str, values: np.ndarray) -> list:
        """Raw categorical values as the vocabulary stores them (text codes decoded)."""
        if name in TEXT_COLUMNS:
            return [self.dictionary[name][c] for c in values.tolist()]
        return [int(v) for v in values.tolist()]

    def encode(self, chunk: dict[str, np.ndarray], vocab: dict[str, list]) -> dict[str, np.ndarray]:
        """Features of a chunk, categoricals coded against `vocab`, numerics as `numeric_features`.

        Equals `encode_categoricals(compute_features(raw), vocab)` for the same calls.
        """
        out = self.numeric(chunk)
        for name, values in self.categorical_values(chunk).items():
            if name in TEXT_COLUMNS:
                lookup = encode_column(np.array(self.dictionary[name], dtype=object), vocab[name])
                out[name] = lookup[values] if len(lookup) else np.full(len(values), UNKNOWN, np.int32)
            else:
                out[name] = encode_column(values.astype(np.int64), vocab[name])
        return out

    def vocabulary(self, cats: dict[str, np.ndarray]) -> dict[str, list]:
        """`build_vocabularies` of raw categorical values (text as codes)."""
        return {name: sorted(self.text_values(name, np.unique(cats[name]))) for name in CATEGORICAL}

    def load(self, dates: Collection[str] | None = None) -> Dataset:
        """The rows on `dates` as an arrival-only `Dataset`, vocabulary from those rows.

        For evaluation-sized ranges; training reads `chunks` instead.
        """
        parts = list(self.chunks(dates))
        col = {name: np.concatenate([p[name] for p in parts]) if parts else np.zeros(0, COLUMNS[name])
               for name in ["day", *COLUMNS]}
        vocab = self.vocabulary(self.categorical_values(col))
        features = self.encode(col, vocab)
        present = np.unique(col["day"])
        dates_present = [str(d) for d in present.astype("datetime64[D]")]
        day = np.searchsorted(present, col["day"]).astype(np.int16)
        scheduled = col["scheduled_arrival_utc"]
        meta = {
            "kind": "arrival-only", "source": "arrival-full", "db_path": self.manifest["db_path"],
            "db_fingerprint": self.manifest["db_fingerprint"], "built_at": self.manifest["built_at"],
            "n_rows": int(len(day)), "n_arr_targets": int(len(day)), "n_dep_targets": 0,
            "dates": dates_present,
            "rows_per_date": {d: int(c) for d, c in zip(dates_present, np.bincount(day, minlength=len(present)))},
            "vocab": vocab, "timetable_asof_verified": False, "cutoff_hours": CUTOFF_HOURS,
        }
        return Dataset(
            features=features, arr_min=col["arr_min"],
            dep_min=np.full(len(day), np.nan, dtype=np.float32), day=day,
            trip_id=col["trip_id"], segment=self.text("segment", col["segment"]), meta=meta,
            stop_id=col["stop_id"], stop_sequence=col["stop_sequence"],
            scheduled_arrival_utc=scheduled, cutoff_utc=scheduled - CUTOFF_HOURS * 3600,
            last_observed_at=col["last_observed_at"], finalized_at=col["finalized_at"],
            v2_sched_local_sec=col["v2_sched_local_sec"].astype(np.int64),
        )


def history_is_stale(manifest: dict, db_path: Path) -> bool:
    return fingerprint(db_path) != manifest["db_fingerprint"]
