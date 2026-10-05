"""Notice-rate model inputs: how often a station, line or train has construction or disruption notices.

A rate is the share of a key's past arrivals whose trip had a notice, smoothed toward the rate one
level up (train -> line -> station -> all stations) so a rarely seen key stays stable:

    rate = (k + WEIGHT * parent) / (n + WEIGHT)

A key with no arrivals is NaN, never 0 (the model's input layout then carries a `_missing`
indicator). The counts come from `notice_rate_daily` (`derive/notice_rates.py`), summed over the
model's training dates only.

Training rows use a leave-one-month-out fold: their own calendar month's counts are subtracted, so a
notice written because a train was already late never reaches the arrival it belongs to.
Validation rows, test rows and live prediction use the totals over all training dates.

One code path serves training, scoring and prediction; the artifact records the training dates, the
weight and a fingerprint of the counts, and `from_record` refuses data that no longer matches.
"""

from __future__ import annotations

import hashlib
import threading

import numpy as np
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from dbrel.ml.features import NOTICE_LEVELS as LEVELS
from dbrel.ml.features import NOTICE_RATES as RATE_NAMES

KINDS = ("works", "disruption")
WEIGHT = 20
CHUNK = 500_000
_FIELDS = ("n", "k_works", "k_disruption")


class NoticeRatesMismatch(RuntimeError):
    """The stored notice counts differ from the ones a model was trained with."""


def month_index(day: np.ndarray) -> np.ndarray:
    """Months since 1970-01 of days since 1970-01-01."""
    return np.asarray(day, dtype="int64").astype("datetime64[D]").astype("datetime64[M]").astype("int64")


def _empty() -> np.ndarray:
    return np.zeros((3, 0), dtype=np.int64)


class NoticeRates:
    """Per-key notice counts over a set of training dates, and the rates they give."""

    def __init__(self, total: dict[str, np.ndarray], by_month: dict[str, dict[int, np.ndarray]],
                 dates: list[str], weight: int = WEIGHT):
        self.total = total  # level -> (3, K) counts: arrivals, with works, with disruption
        self.by_month = by_month  # level -> month index -> (3, K)
        self.dates = list(dates)
        self.weight = weight
        if not total["station"][0].sum():
            raise ValueError("no counted arrivals on the training dates; build-notice-rates first")

    # -- loading ---------------------------------------------------------------------------------

    @classmethod
    def from_database(cls, engine: Engine, dates: list[str], weight: int = WEIGHT) -> NoticeRates:
        """Sum `notice_rate_daily` over exactly `dates` (ISO strings)."""
        dates = sorted(set(dates))
        if not dates:
            raise ValueError("notice rates need at least one training date")
        wanted = np.array(dates, dtype="datetime64[D]").astype("int64")
        total: dict[str, np.ndarray] = {}
        by_month: dict[str, dict[int, np.ndarray]] = {}
        with engine.connect() as conn:
            for level in LEVELS:
                (top,) = conn.exec_driver_sql(
                    "SELECT COALESCE(MAX(key_id), -1) FROM notice_rate_daily WHERE level = ? "
                    "AND service_date >= ? AND service_date <= ?", (level, dates[0], dates[-1])).fetchone()
                size = int(top) + 1
                total[level] = np.zeros((3, size), dtype=np.int64)
                by_month[level] = {}
                cursor = conn.exec_driver_sql(
                    "SELECT key_id, service_date, n, k_works, k_disruption FROM notice_rate_daily "
                    "WHERE level = ? AND service_date >= ? AND service_date <= ?",
                    (level, dates[0], dates[-1]))
                while chunk := cursor.fetchmany(CHUNK):
                    key = np.array([r[0] for r in chunk], dtype=np.int64)
                    day = np.array([r[1] for r in chunk], dtype="datetime64[D]").astype("int64")
                    counts = np.array([r[2:] for r in chunk], dtype=np.int64)
                    keep = np.isin(day, wanted)
                    key, day, counts = key[keep], day[keep], counts[keep]
                    month = month_index(day)
                    for m in np.unique(month):
                        sel = month == m
                        acc = by_month[level].setdefault(int(m), np.zeros((3, size), dtype=np.int64))
                        for row, field in enumerate(counts[sel].T):
                            np.add.at(acc[row], key[sel], field)
                    for row, field in enumerate(counts.T):
                        np.add.at(total[level][row], key, field)
        return cls(total, by_month, dates, weight)

    @classmethod
    def from_record(cls, engine: Engine, record: dict) -> NoticeRates:
        """The rates an artifact was trained with; refuses if the stored counts have changed."""
        rates = cls.from_database(engine, record["dates"], record["weight"])
        rates._check(record)
        return rates

    @classmethod
    def from_totals(cls, engine: Engine, record: dict) -> NoticeRates:
        """The same rates from `notice_rate_total`, the per-key sums a deployed (slim) database holds.

        Only the totals are stored there, so leave-one-month-out lookups are not available (serving
        never uses them). The fingerprint check is the same as `from_record`'s.
        """
        total: dict[str, np.ndarray] = {}
        with engine.connect() as conn:
            for level in LEVELS:
                (size,) = conn.exec_driver_sql(
                    "SELECT size FROM notice_rate_total_meta WHERE level = ?", (level,)).fetchone()
                total[level] = np.zeros((3, int(size)), dtype=np.int64)
                rows = conn.exec_driver_sql(
                    "SELECT key_id, n, k_works, k_disruption FROM notice_rate_total WHERE level = ?",
                    (level,)).fetchall()
                if rows:
                    counts = np.array(rows, dtype=np.int64)
                    total[level][:, counts[:, 0]] = counts[:, 1:].T
        rates = cls(total, {level: {} for level in LEVELS}, record["dates"], record["weight"])
        rates._check(record)
        return rates

    def _check(self, record: dict) -> None:
        """Raise NoticeRatesMismatch unless these counts give `record`'s fingerprint."""
        if self.fingerprint() != record["fingerprint"]:
            raise NoticeRatesMismatch(
                f"the notice counts for {record['dates'][0]} .. {record['dates'][-1]} "
                f"({len(record['dates'])} dates) differ from the ones this model was trained with "
                "(the table was rebuilt or edited since); restore those rows or retrain the model")

    def peak_bytes(self) -> int:
        """Memory of the counts, plus the one largest-level copy a leave-one-month-out lookup makes."""
        stored = sum(a.nbytes for a in self.total.values())
        stored += sum(a.nbytes for months in self.by_month.values() for a in months.values())
        return stored + max(a.nbytes for a in self.total.values())

    # -- identity --------------------------------------------------------------------------------

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(f"{self.weight}|{self.dates[0]}|{self.dates[-1]}|{len(self.dates)}".encode())
        for level in LEVELS:
            digest.update(level.encode())
            digest.update(np.ascontiguousarray(self.total[level]).tobytes())
        return digest.hexdigest()

    def record(self) -> dict:
        """What the artifact's manifest keeps so serving can rebuild and check these rates."""
        return {"dates": self.dates, "weight": self.weight, "fingerprint": self.fingerprint()}

    # -- rates -----------------------------------------------------------------------------------

    def _counts(self, level: str, key: np.ndarray, month: int | None) -> np.ndarray:
        """(3, len(key)) counts, outside `month` when given; zeros for keys the table never saw."""
        table = self.total[level]
        if month is not None and month in self.by_month[level]:
            table = table - self.by_month[level][month]
        valid = (key >= 0) & (key < table.shape[1])
        out = np.zeros((3, len(key)), dtype=np.int64)
        out[:, valid] = table[:, key[valid]]
        return out

    def _smooth(self, counts: np.ndarray, kind: int, parent: np.ndarray) -> np.ndarray:
        n, k = counts[0], counts[kind]
        rate = (k + self.weight * parent) / (n + self.weight)
        return np.where(n > 0, rate, np.nan)

    def rates(self, station_id, line_id, train_id, day, *, fold: bool) -> dict[str, np.ndarray]:
        """The six rate columns (float32, NaN where unknown) for rows given as parallel arrays.

        `day` is days since 1970-01-01. With `fold=True` (training rows) each row's own calendar
        month is left out; otherwise every training date counts.
        """
        station_id, line_id, train_id = (np.asarray(a, dtype=np.int64) for a in (station_id, line_id, train_id))
        day = np.asarray(day, dtype=np.int64)
        out = {name: np.full(len(day), np.nan, dtype=np.float32) for name in RATE_NAMES}
        months = month_index(day) if fold else np.zeros(len(day), dtype=np.int64)
        for m in np.unique(months):
            sel = months == m
            month = int(m) if fold else None
            station = self._counts("station", station_id[sel], month)
            line = self._counts("line", line_id[sel], month)
            train = self._counts("train", train_id[sel], month)
            everyone = self._counts("station", np.arange(self.total["station"].shape[1]), month)
            for kind_pos, kind in enumerate(KINDS, start=1):
                overall = everyone[kind_pos].sum() / max(1, everyone[0].sum())
                station_rate = self._smooth(station, kind_pos, overall)
                parent = np.where(np.isnan(station_rate), overall, station_rate)
                line_rate = self._smooth(line, kind_pos, parent)
                parent = np.where(np.isnan(line_rate), parent, line_rate)
                train_rate = self._smooth(train, kind_pos, parent)
                for level, values in zip(LEVELS, (station_rate, line_rate, train_rate)):
                    out[f"{kind}_rate_{level}"][sel] = values
        return out


def table_signature(engine: Engine, first: str, last: str) -> dict:
    """A cheap summary of `notice_rate_daily` between two dates, for checkpoint keys.

    Counts and sums per level; changes whenever a count in the range changes. The exact check a
    model makes at load time is `NoticeRates.fingerprint`.
    """
    out = {}
    with engine.connect() as conn:
        for level in LEVELS:
            row = conn.exec_driver_sql(
                "SELECT COUNT(*), COALESCE(SUM(n), 0), COALESCE(SUM(k_works), 0), "
                "COALESCE(SUM(k_disruption), 0) FROM notice_rate_daily "
                "WHERE level = ? AND service_date >= ? AND service_date <= ?", (level, first, last)).fetchone()
            out[level] = [int(v) for v in row]
    return out


def write_totals(engine: Engine, rates: NoticeRates) -> int:
    """Store `rates`' per-key totals (and the array sizes the fingerprint needs) in a deployable database.

    Creates `notice_rate_total` and `notice_rate_total_meta`, replacing earlier contents; returns the
    number of key rows written. `NoticeRates.from_totals` reads them back.
    """
    rows = 0
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS notice_rate_total")
        conn.exec_driver_sql("DROP TABLE IF EXISTS notice_rate_total_meta")
        conn.exec_driver_sql("""CREATE TABLE notice_rate_total (level TEXT NOT NULL, key_id INTEGER NOT NULL,
            n INTEGER NOT NULL, k_works INTEGER NOT NULL, k_disruption INTEGER NOT NULL,
            PRIMARY KEY (level, key_id)) WITHOUT ROWID""")
        conn.exec_driver_sql("CREATE TABLE notice_rate_total_meta (level TEXT PRIMARY KEY, size INTEGER NOT NULL)")
        for level in LEVELS:
            table = rates.total[level]
            conn.exec_driver_sql("INSERT INTO notice_rate_total_meta VALUES (?, ?)", (level, int(table.shape[1])))
            seen = np.flatnonzero(table.any(axis=0))
            conn.exec_driver_sql(
                "INSERT INTO notice_rate_total VALUES (?, ?, ?, ?, ?)",
                [(level, int(k), *(int(v) for v in table[:, k])) for k in seen])
            rows += len(seen)
    return rows


# -- serving ----------------------------------------------------------------------------------------

_serving: dict[tuple[str, str], NoticeRates] = {}
_serving_lock = threading.Lock()  # a page fires several predictions at once; only one may load the table


def uses_notice_rates(manifest: dict) -> bool:
    """Whether an artifact's input list asks for the notice rates (older artifacts never do)."""
    return any(name in RATE_NAMES for name in manifest.get("prep", {}).get("features_numeric", ()))


def serving_rates(manifest: dict, engine: Engine) -> NoticeRates:
    """The rates this artifact was trained with, loaded once per process and checked on load."""
    record = manifest.get("notice_rates")
    if record is None:
        raise ValueError("this artifact lists notice-rate inputs but records no notice_rates")
    key = (str(engine.url), record["fingerprint"])
    if key not in _serving:
        with _serving_lock:
            if key not in _serving:
                # A deployed slim database holds only the per-key totals, not the daily table.
                load = NoticeRates.from_totals if inspect(engine).has_table("notice_rate_total") else NoticeRates.from_record
                _serving[key] = load(engine, record)
    return _serving[key]


def add_serving_rates(features: dict[str, np.ndarray], raw: dict, manifest: dict, engine: Engine) -> dict:
    """`features` plus the six rate columns when the artifact uses them; otherwise unchanged.

    `raw` holds the call's station_id, line_id (-1 without a line) and train_id. Prediction uses the
    totals over all training dates, so the day does not matter.
    """
    if not uses_notice_rates(manifest):
        return features
    station, line, train = (np.asarray(raw[name], dtype=np.int64) for name in ("station_id", "line_id", "train_id"))
    rates = serving_rates(manifest, engine).rates(station, line, train, np.zeros(len(station), dtype=np.int64),
                                                  fold=False)
    return {**features, **rates}
