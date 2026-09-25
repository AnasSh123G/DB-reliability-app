"""Build the training dataset: one row per trustworthy observed stop.

A row becomes a target only if its observation is recorded (not a forecast or
missing), the stop is not cancelled, and its holiday flag is decidable. Each
side (arrival, departure) is then judged on its own: a delay missing or beyond
`MAX_PLAUSIBLE_DELAY_S` leaves that side's target NaN without discarding the
other side. These are the averages builder's filters, so the averages baseline
and the model candidates are scored on the same population.

The result is written to `data/ml/dataset.npz` (column arrays) and
`dataset.json` (vocabularies, dates, counts, and a fingerprint of the
database's contents so a stale dataset is caught before hours of training go
into it). The fingerprint reads the data, not the file: in WAL mode a commit
can leave the main file's mtime and size untouched, and a checkpoint can change
them without any data changing.
Materialising once is the point: the bake-off reads the data dozens of times.

Categoricals are encoded against a vocabulary of *every* value in the dataset;
the bake-off narrows it to the training days before fitting (`split.py`), so a
train first seen on a test day is UNKNOWN to the model -- exactly as it would be
at prediction time.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dbrel.derive.berlin_time_sql import offset_breakpoints
from dbrel.derive.stop_averages import _TRIP_START_TTS_SQL, MAX_PLAUSIBLE_DELAY_S
from dbrel.ml.features import (
    NO_LINE,
    RAW_COLUMNS,
    UNKNOWN_DIRECTION,
    build_vocabularies,
    compute_features,
    encode_categoricals,
)

DATASET_NAME = "dataset.npz"
META_NAME = "dataset.json"
_CHUNK = 250_000

_QUERY = f"""
    SELECT
        ts.trip_id,
        t.service_date,
        ts.station_id,
        tr.id,
        tr.line_id,
        tr.category,
        tr.operator,
        tr.segment,
        st.state,
        st.lat,
        st.lon,
        ts.is_holiday,
        COALESCE(ts.supposed_departure_utc, ts.supposed_arrival_utc),
        ({_TRIP_START_TTS_SQL}),
        r.stop_rank,
        r.n_stops,
        ts.arr_delay_s,
        ts.dep_delay_s
    FROM trip_stop ts
    JOIN trip t ON t.id = ts.trip_id
    JOIN train tr ON tr.id = t.train_id
    JOIN station st ON st.id = ts.station_id
    LEFT JOIN ml_stop_rank r ON r.id = ts.station_stop_id
    WHERE ts.quality = 'RECORDED'
      AND ts.cancelled = 0
      AND ts.is_holiday IS NOT NULL
      AND COALESCE(ts.supposed_departure_utc, ts.supposed_arrival_utc) IS NOT NULL
      AND (
            (ts.arr_delay_s IS NOT NULL AND ABS(ts.arr_delay_s) <= {MAX_PLAUSIBLE_DELAY_S})
         OR (ts.dep_delay_s IS NOT NULL AND ABS(ts.dep_delay_s) <= {MAX_PLAUSIBLE_DELAY_S})
      )
"""

# The window prediction's `timetable.calls_at` reproduces in Python.
_RANK_TABLE_SQL = """
    CREATE TEMP TABLE ml_stop_rank AS
    SELECT id,
           ROW_NUMBER() OVER w - 1 AS stop_rank,
           COUNT(*) OVER (PARTITION BY feed_version_id, train_id, path_sha1) AS n_stops
    FROM station_stop
    WINDOW w AS (PARTITION BY feed_version_id, train_id, path_sha1 ORDER BY stop_sequence)
"""


@dataclass
class Dataset:
    """The materialised dataset, as loaded by the bake-off."""

    features: dict[str, np.ndarray]  # encoded, keyed by features.FEATURES
    arr_min: np.ndarray  # float32 target, NaN where not a target
    dep_min: np.ndarray
    day: np.ndarray  # int16 index into meta["dates"]
    trip_id: np.ndarray  # int64
    segment: np.ndarray  # object: FV / RV / S / ""
    meta: dict


def _hhmm_to_sec(values: list) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for i, value in enumerate(values):
        if value:
            out[i] = int(value[:2]) * 3600 + int(value[3:5]) * 60
    return out


def _local_sec_of_day(utc: np.ndarray, breakpoints: list[tuple[int, int]]) -> np.ndarray:
    """Berlin-local seconds after midnight for UTC epochs, DST-aware, vectorised."""
    starts = np.array([b for b, _ in breakpoints], dtype=np.int64)
    offsets = np.array([o for _, o in breakpoints], dtype=np.int64)
    idx = np.clip(np.searchsorted(starts, utc, side="right") - 1, 0, len(starts) - 1)
    return (utc + offsets[idx]) % 86400


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-262144")
    return conn


def build_dataset(db_path: Path, out_dir: Path, directions: dict[int, int], *, progress=None) -> dict:
    """Read every target row, compute features, write the dataset. Returns the meta dict.

    `directions` is `features.all_directions(session)` -- resolved by the
    caller, which has an ORM session; this function only speaks raw sqlite3.
    """
    started = time.monotonic()
    conn = _connect_readonly(db_path)
    conn.execute("DROP TABLE IF EXISTS temp.ml_stop_rank")
    conn.execute(_RANK_TABLE_SQL)
    conn.execute("CREATE UNIQUE INDEX temp.ml_stop_rank_id ON ml_stop_rank(id)")

    years = [int(y) for (y,) in conn.execute("SELECT DISTINCT substr(service_date, 1, 4) FROM trip")]
    breakpoints = offset_breakpoints(years)

    chunks: dict[str, list[np.ndarray]] = {}
    cursor = conn.execute(_QUERY)
    n_rows = 0
    while True:
        rows = cursor.fetchmany(_CHUNK)
        if not rows:
            break
        cols = list(zip(*rows))
        (trip_id, service_date, station_id, train_id, line_id, category, operator, segment,
         state, lat, lon, holiday, sched_utc, trip_start, stop_rank, n_stops, arr_s, dep_s) = cols

        arr = np.array([np.nan if v is None else v for v in arr_s], dtype=np.float64)
        dep = np.array([np.nan if v is None else v for v in dep_s], dtype=np.float64)
        arr[np.abs(arr) > MAX_PLAUSIBLE_DELAY_S] = np.nan
        dep[np.abs(dep) > MAX_PLAUSIBLE_DELAY_S] = np.nan

        piece = {
            "trip_id": np.array(trip_id, dtype=np.int64),
            "service_date": np.array(service_date, dtype=object),
            "station_id": np.array(station_id, dtype=np.int64),
            "train_id": np.array(train_id, dtype=np.int64),
            "line_id": np.array([NO_LINE if v is None else v for v in line_id], dtype=np.int64),
            "category": np.array(category, dtype=object),
            "operator": np.array(operator, dtype=object),
            "segment": np.array(["" if v is None else v for v in segment], dtype=object),
            "state": np.array(state, dtype=object),
            "lat": np.array(lat, dtype=np.float64),
            "lon": np.array(lon, dtype=np.float64),
            "is_holiday": np.array(holiday, dtype=np.int8),
            "sched_local_sec": _local_sec_of_day(np.array(sched_utc, dtype=np.int64), breakpoints),
            "trip_start_sec": _hhmm_to_sec(trip_start),
            "stop_rank": np.array([np.nan if v is None else v for v in stop_rank], dtype=np.float64),
            "n_stops": np.array([np.nan if v is None else v for v in n_stops], dtype=np.float64),
            "arr_min": (arr / 60).astype(np.float32),
            "dep_min": (dep / 60).astype(np.float32),
        }
        for key, value in piece.items():
            chunks.setdefault(key, []).append(value)
        n_rows += len(rows)
        if progress:
            progress(n_rows)
    conn.close()

    col = {key: np.concatenate(parts) for key, parts in chunks.items()}
    dates = sorted(set(col["service_date"].tolist()))
    date_index = {d: i for i, d in enumerate(dates)}
    day = np.array([date_index[d] for d in col["service_date"]], dtype=np.int16)
    weekday = np.array([np.datetime64(d).astype("datetime64[D]").item().weekday() for d in dates])[day]

    raw = {name: col[name] for name in RAW_COLUMNS if name in col}
    raw["weekday"] = weekday
    raw["direction"] = np.array(
        [directions.get(t, UNKNOWN_DIRECTION) for t in col["train_id"].tolist()], dtype=np.int64
    )
    features = compute_features({name: raw[name] for name in RAW_COLUMNS})
    vocab = build_vocabularies(features)
    encoded = encode_categoricals(features, vocab)

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (DATASET_NAME + ".tmp.npz")
    np.savez(
        tmp,
        **{f"f_{name}": encoded[name] for name in encoded},
        arr_min=col["arr_min"],
        dep_min=col["dep_min"],
        day=day,
        trip_id=col["trip_id"],
        segment=col["segment"].astype(str),
    )
    tmp.replace(out_dir / DATASET_NAME)

    per_day = np.bincount(day, minlength=len(dates))
    meta = {
        "db_path": str(db_path),
        "db_fingerprint": fingerprint(db_path),
        "built_at": time.time(),
        "build_seconds": round(time.monotonic() - started, 1),
        "n_rows": int(n_rows),
        "n_arr_targets": int(np.count_nonzero(~np.isnan(col["arr_min"]))),
        "n_dep_targets": int(np.count_nonzero(~np.isnan(col["dep_min"]))),
        "dates": dates,
        "rows_per_date": {d: int(c) for d, c in zip(dates, per_day)},
        "vocab": vocab,
    }
    (out_dir / META_NAME).write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    return meta


def load_dataset(out_dir: Path) -> Dataset:
    meta = json.loads((out_dir / META_NAME).read_text())
    with np.load(out_dir / DATASET_NAME, allow_pickle=False) as data:
        features = {key[2:]: data[key] for key in data.files if key.startswith("f_")}
        return Dataset(
            features=features,
            arr_min=data["arr_min"],
            dep_min=data["dep_min"],
            day=data["day"],
            trip_id=data["trip_id"],
            segment=data["segment"],
            meta=meta,
        )


_FINGERPRINT_SQL = (
    "SELECT (SELECT COUNT(*) FROM trip_stop), (SELECT MAX(id) FROM trip_stop), "
    "(SELECT COUNT(*) FROM trip), (SELECT MAX(finalized_at) FROM trip), "
    "(SELECT COUNT(*) FROM station_stop), (SELECT COUNT(*) FROM train), "
    "(SELECT COUNT(*) FROM trip_stop WHERE is_holiday IS NULL)"
)


def fingerprint(db_path: Path) -> list:
    """Row counts and high-water marks of the tables the dataset is built from.

    Catches new or deleted observations, re-finalised trips, a timetable
    change and a holiday relabel -- the things that change what build-dataset
    would produce. An in-place edit of one delay value would slip through;
    nothing in dbrel does that outside a rebuild.
    """
    conn = _connect_readonly(db_path)
    try:
        return list(conn.execute(_FINGERPRINT_SQL).fetchone())
    finally:
        conn.close()


def dataset_is_stale(meta: dict, db_path: Path) -> bool:
    """Whether the database changed since the dataset was built from it."""
    return fingerprint(db_path) != meta["db_fingerprint"]
