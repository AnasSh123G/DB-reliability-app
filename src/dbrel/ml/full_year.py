"""The full-year arrival pipeline between the monthly shards and a fit (no torch here).

Each step streams the shards (`full_history`), so memory does not grow with
the months:

1. `evaluation_cutoff` -- the earliest forecast cutoff among the evaluated
   rows. Training keeps only runs finalized by then, the rule of
   `split.training_history_before`.
2. `training_statistics` -- one pass over the training rows: per-value counts
   and last-seen days of every categorical, and numeric moments plus a seeded
   sample for quantiles (exact when the training rows fit in the sample).
3. `split.training_vocabulary` and `prep_from_statistics` -- the vocabulary
   and input prep a model is trained, exported and served with.
4. `prepare` -- the rows' model inputs written once to memory-mapped files,
   reused while the key (dataset, rows, vocabulary, prep) is unchanged and
   refused, with the differences named, when it is not.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dbrel.ml.arrival import SCALINGS, arrival_inputs, prep_layout
from dbrel.ml.checkpoint import CheckpointMismatch, _write_json, key_differences
from dbrel.ml.features import ARRIVAL_NUMERIC, CATEGORICAL, SEASONAL
from dbrel.ml.full_history import COLUMNS, CUTOFF_HOURS, CODE_VERSION, FullHistory
from dbrel.ml.metrics import score_arrivals_with_breakdowns

SAMPLE_ROWS = 2_000_000
PREPARED_VERSION = 1
KEY_NAME = "key.json"
META_NAME = "meta.json"
# name -> (dtype, columns per row; 0 = one value)
PREPARED_COLUMNS = {
    "cats": ("<i4", len(CATEGORICAL)),
    "nums": ("<f4", None),  # len(prep["numeric"])
    "y": ("<f4", 0),
    "stop_id": ("<i8", 0),
    "day": ("<i4", 0),
    "segment": ("<i2", 0),
    "sched_min": ("<f4", 0),  # unscaled, for the histogram baseline's hour key
    "is_holiday": ("<f4", 0),
}
HISTOGRAM_CATEGORICALS = ("train", "station", "line", "category", "direction")


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class RowSet:
    """Rows a step reads: these dates, and for training only runs finalized by `finalized_before`."""

    dates: tuple[str, ...]
    finalized_before: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "dates", tuple(sorted(self.dates)))
        if not self.dates:
            raise ValueError("a row set needs at least one date")

    def chunks(self, history: FullHistory, names: Collection[str] = COLUMNS) -> Iterator[dict[str, np.ndarray]]:
        names = set(names) | ({"finalized_at"} if self.finalized_before is not None else set())
        for chunk in history.chunks(self.dates, names):
            if self.finalized_before is not None:
                f = chunk["finalized_at"]
                keep = (f > 0) & (f <= self.finalized_before)
                if not keep.any():
                    continue
                chunk = {k: v[keep] for k, v in chunk.items()}
            yield chunk

    def as_dict(self) -> dict:
        return {"first": self.dates[0], "last": self.dates[-1], "n_dates": len(self.dates),
                "dates_sha": _sha(self.dates), "finalized_before": self.finalized_before}


def history_key(history: FullHistory) -> dict:
    """What the shards' contents depend on -- not when they were last written."""
    shards = history.manifest["shards"]
    return {"code_version": CODE_VERSION,
            "shards": {m: [s["rows"], s["fingerprint"]] for m, s in shards.items()}}


def evaluation_cutoff(history: FullHistory, dates: Collection[str]) -> int:
    """UTC second of the earliest forecast cutoff among the rows on `dates`."""
    lowest = None
    for chunk in history.chunks(dates, ["scheduled_arrival_utc"]):
        if len(chunk["scheduled_arrival_utc"]):
            m = int(chunk["scheduled_arrival_utc"].min())
            lowest = m if lowest is None else min(lowest, m)
    if lowest is None:
        raise ValueError(f"no arrivals on the evaluated dates {min(dates)} .. {max(dates)}")
    return lowest - CUTOFF_HOURS * 3600


def _merge_moments(acc: tuple[int, float, float], x: np.ndarray) -> tuple[int, float, float]:
    """Chan et al.'s pairwise update of (count, mean, sum of squared deviations)."""
    if not len(x):
        return acc
    n_a, mean_a, m2_a = acc
    n_b, mean_b = len(x), float(x.mean())
    m2_b = float(((x - mean_b) ** 2).sum())
    n = n_a + n_b
    delta = mean_b - mean_a
    return n, mean_a + delta * n_b / n, m2_a + m2_b + delta * delta * n_a * n_b / n


def training_statistics(history: FullHistory, rows: RowSet, *, numeric: tuple[str, ...] = ARRIVAL_NUMERIC,
                        sample_rows: int = SAMPLE_ROWS, seed: int = 0) -> dict:
    """Category and numeric statistics of the training rows, in one streamed pass."""
    expected = sum(history.rows_per_date.get(d, 0) for d in rows.dates)
    share = min(1.0, sample_rows / max(1, expected))
    rng = np.random.default_rng(seed)
    cats_acc: dict[str, dict] = {name: {} for name in CATEGORICAL}
    moments = {name: (0, 0.0, 0.0) for name in numeric}
    missing = dict.fromkeys(numeric, False)
    samples: dict[str, list] = {name: [] for name in numeric}
    n_rows, last_day = 0, None
    for chunk in rows.chunks(history):
        day = chunk["day"].astype(np.int64)
        n_rows += len(day)
        last_day = int(day.max()) if last_day is None else max(last_day, int(day.max()))
        for name, values in history.categorical_values(chunk).items():
            uniq, inv, counts = np.unique(values, return_inverse=True, return_counts=True)
            last = np.full(len(uniq), np.iinfo(np.int64).min, dtype=np.int64)
            np.maximum.at(last, inv, day)
            acc = cats_acc[name]
            for v, c, lday in zip(uniq.tolist(), counts.tolist(), last.tolist()):
                old = acc.get(v)
                acc[v] = (c, lday) if old is None else (old[0] + c, max(old[1], lday))
        feats = history.numeric(chunk)
        take = rng.random(len(day)) < share if share < 1 else slice(None)
        for name in numeric:
            v = feats[name].astype(np.float64)
            ok = ~np.isnan(v)
            missing[name] |= bool((~ok).any())
            moments[name] = _merge_moments(moments[name], v[ok])
            samples[name].append(v[take])
    if not n_rows:
        raise ValueError(f"no finalized training arrivals on {rows.dates[0]} .. {rows.dates[-1]}")
    categories = {}
    for name, acc in cats_acc.items():
        codes = np.array(list(acc), dtype=np.int64)
        categories[name] = (history.text_values(name, codes), np.array([c for c, _ in acc.values()]),
                            np.array([d for _, d in acc.values()], dtype=np.int64))
    numeric_stats = {}
    for name in numeric:
        count, mean, m2 = moments[name]
        if not count:
            raise ValueError(f"{name} is missing on every training row")
        q25, q50, q75 = (float(x) for x in np.nanquantile(np.concatenate(samples[name]), [0.25, 0.5, 0.75]))
        numeric_stats[name] = {"mean": mean, "std": float(np.sqrt(m2 / count)), "q25": q25, "q50": q50,
                               "q75": q75, "missing": missing[name]}
    return {"rows": n_rows, "last_day": last_day, "sample_share": share, "categories": categories,
            "numeric": numeric_stats}


def prep_from_statistics(stats: dict, vocab: dict[str, list], scaling: str,
                         numeric: tuple[str, ...] = ARRIVAL_NUMERIC) -> dict:
    """`arrival.fit_input_prep`'s result, from `training_statistics` instead of in-memory rows."""
    if scaling not in SCALINGS:
        raise ValueError(f"unknown scaling {scaling!r}")
    centre_scale = {}
    for name in numeric:
        s = stats["numeric"][name]
        if name in SEASONAL:
            centre_scale[name] = [0.0, 1.0]
            continue
        centre, scale = (s["mean"], s["std"]) if scaling == "standard" else \
            (s["q50"], (s["q75"] - s["q25"]) / 1.349)
        centre_scale[name] = [centre, scale if scale > 0 else 1.0]
    return prep_layout(scaling, [len(vocab[c]) + 1 for c in CATEGORICAL], tuple(numeric), centre_scale,
                       {name: stats["numeric"][name]["missing"] for name in numeric})


class FileColumn:
    """A column file whose row slices are read with plain file reads.

    Unlike a memmap, nothing stays mapped: pages read go to the (reclaimable)
    page cache, not the process's resident set, so an epoch over a 10 GB file
    does not show up as 10 GB of RSS in the memory probe or the job's peak.
    """

    def __init__(self, path: Path, dtype: str, n: int, width: int):
        self.path, self.dtype, self.n, self.width = Path(path), np.dtype(dtype), n, width
        self.shape = (n, width) if width else (n,)

    def __len__(self) -> int:
        return self.n

    def __getitem__(self, index: slice) -> np.ndarray:
        if not isinstance(index, slice) or index.step not in (None, 1):
            raise TypeError("a file column reads contiguous row slices only")
        start, stop, _ = index.indices(self.n)
        count = max(0, stop - start)
        per_row = max(1, self.width)
        if not count:
            return np.zeros((0, *self.shape[1:]), dtype=self.dtype)
        data = np.fromfile(self.path, dtype=self.dtype, count=count * per_row,
                           offset=start * per_row * self.dtype.itemsize)
        return data.reshape(count, self.width) if self.width else data

    def __array__(self, dtype=None, copy=None):
        out = self[:]
        return out if dtype is None else out.astype(dtype)


class PreparedSet:
    """The model inputs of one row set, as `prepare` wrote them."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.meta = json.loads((self.dir / META_NAME).read_text())
        self.key = json.loads((self.dir / KEY_NAME).read_text())
        self.n = self.meta["n"]

    def __len__(self) -> int:
        return self.n

    def column(self, name: str) -> FileColumn:
        dtype, width = PREPARED_COLUMNS[name]
        return FileColumn(self.dir / f"{name}.bin", dtype, self.n, self.meta["n_num"] if width is None else width)

    @property
    def cats(self) -> FileColumn:
        return self.column("cats")

    @property
    def nums(self) -> FileColumn:
        return self.column("nums")

    @property
    def y(self) -> FileColumn:
        return self.column("y")

    def histogram_encoded(self, block_rows: int = 1 << 20) -> dict[str, np.ndarray]:
        """The histogram baseline's inputs: its key components are the network's category slots.

        Filled block by block, so the full (rows x 8) category array is never in memory.
        """
        out = {name: np.empty(self.n, dtype=np.int32) for name in HISTOGRAM_CATEGORICALS}
        cats = self.cats
        for s in range(0, self.n, block_rows):
            block = cats[s:s + block_rows]
            for name in HISTOGRAM_CATEGORICALS:
                out[name][s:s + len(block)] = block[:, CATEGORICAL.index(name)] - 1
        out["sched_min"] = np.asarray(self.column("sched_min"))
        out["is_holiday"] = np.asarray(self.column("is_holiday"))
        return out


def prepare(history: FullHistory, rows: RowSet, vocab: dict[str, list], prep: dict, root: Path,
            name: str, *, replace: bool = False, log=None) -> PreparedSet:
    """Write (or reuse) the model inputs of `rows` under `root/name`."""
    key = {"version": PREPARED_VERSION, "history": history_key(history), "rows": rows.as_dict(),
           "vocab": _sha(vocab), "prep": _sha(prep)}
    directory = Path(root) / name
    if (directory / KEY_NAME).exists():
        saved = json.loads((directory / KEY_NAME).read_text())
        if saved == key:
            return PreparedSet(directory)
        if not replace:
            raise CheckpointMismatch(f"prepared inputs in {directory} are for a different setup -- "
                                     + "; ".join(key_differences(saved, key)))
        shutil.rmtree(directory)
    partial = Path(root) / f".{name}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    n_num = len(prep["numeric"])
    files = {c: (partial / f"{c}.bin").open("wb") for c in PREPARED_COLUMNS}
    n = 0
    try:
        for chunk in rows.chunks(history):
            enc = history.encode(chunk, vocab)
            cats, nums = arrival_inputs(enc, prep)
            values = {"cats": cats, "nums": nums, "y": chunk["arr_min"], "stop_id": chunk["stop_id"],
                      "day": chunk["day"], "segment": chunk["segment"], "sched_min": enc["sched_min"],
                      "is_holiday": enc["is_holiday"]}
            for c, fh in files.items():
                fh.write(np.ascontiguousarray(values[c], dtype=PREPARED_COLUMNS[c][0]).tobytes())
            n += len(cats)
            if log:
                log(f"  {name}: {n:,} rows written")
    finally:
        for fh in files.values():
            fh.close()
    _write_json(partial / META_NAME, {"n": n, "n_num": n_num, "numeric": prep["numeric"]})
    _write_json(partial / KEY_NAME, key)
    partial.rename(directory)
    return PreparedSet(directory)


def memory_check_path(work: Path, track: str) -> Path:
    """Where `full_year_search.memory_check` writes a track's projection and the launch gate reads it."""
    return Path(work) / f"memory_check-{track}.json"


def month_of_days(day: np.ndarray) -> np.ndarray:
    return np.datetime_as_string(np.asarray(day, dtype="int64").astype("datetime64[D]"), unit="M")


def evaluation_report(history: FullHistory, prepared: PreparedSet, predictions: dict[str, np.ndarray]) -> dict:
    """Per-model overall, per-month and per-segment scores on identical rows."""
    actual = np.asarray(prepared.y, dtype=np.float64)
    day = np.asarray(prepared.column("day"))
    groups = {"month": month_of_days(day), "segment": history.text("segment", np.asarray(prepared.column("segment")))}
    out = {}
    for name, point in predictions.items():
        if len(point) != len(actual):
            raise ValueError(f"{name} predicted {len(point)} rows, expected {len(actual)}")
        out[name] = score_arrivals_with_breakdowns(actual, np.asarray(point, dtype=np.float64), groups)
    return out
