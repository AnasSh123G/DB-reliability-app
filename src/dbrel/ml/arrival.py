"""Arrival-only candidates for the individual-arrival experiments (numpy only).

Holds what every candidate shares, plus the one candidate that needs no torch:

- `ArrivalForecast`: one finite arrival point per call, or NaN with an explicit
  missing reason; genuine ordered p25/p50/p75 only when the method has them.
- Decision rules: `median`, or `window` -- the centre of the 6-minute interval
  holding the most predicted probability, which maximises the chance of being
  within 3 minutes if the distribution is right. A window point is never p50.
- `fit_input_prep` / `arrival_inputs`: network inputs fitted on training rows
  only; an unknown or out-of-vocabulary category always goes to slot 0.
- `ConditionalHistogram`: training-only empirical arrival-delay distribution
  per call key, with explicit backoff to broader keys. Day type stays in every
  key except the last, global one.

The networks are in `arrival_nn` (torch; ROCm venv). The deployed two-sided
TabM (`nn.py`) is untouched.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from dbrel.ml.features import CATEGORICAL, NOTICE_RATES, NUMERIC, SEASONAL

POINT_RULES = ("median", "window")
WINDOW_HALF_MIN = 3.0
PERCENTILES = (0.25, 0.5, 0.75)
CYCLIC = ("sched_min", "trip_start_min")
SCALINGS = ("standard", "robust")
_EPS = 1e-6  # keeps float rounding from dropping a value that sits exactly on the window edge


@dataclass(frozen=True)
class ArrivalForecast:
    point: np.ndarray                 # float32 minutes; NaN exactly where missing_reason != ""
    missing_reason: np.ndarray        # str; "" where there is a prediction
    rule: str                         # how the point was chosen, one of POINT_RULES
    percentiles: np.ndarray | None = None  # (n, 3) ordered p25/p50/p75, genuine only
    detail: dict = field(default_factory=dict)  # per-row diagnostics, e.g. backoff level

    def __post_init__(self):
        n = len(self.point)
        if self.point.ndim != 1 or self.missing_reason.shape != (n,):
            raise ValueError("point and missing_reason must be matching 1-D arrays")
        if self.rule not in POINT_RULES:
            raise ValueError(f"unknown point rule {self.rule!r}")
        has = self.missing_reason == ""
        if not np.isfinite(self.point[has]).all():
            raise ValueError("a predicted arrival point must be finite")
        if not np.isnan(self.point[~has]).all():
            raise ValueError("a missing arrival must have a NaN point")
        if self.percentiles is not None:
            p = self.percentiles
            if p.shape != (n, 3):
                raise ValueError("percentiles must be (n, 3)")
            if not np.isfinite(p[has]).all() or np.any(np.diff(p[has], axis=1) < 0):
                raise ValueError("percentiles must be finite and ordered")
            if self.rule == "median" and not np.allclose(self.point[has], p[has, 1]):
                raise ValueError("a median point must equal p50")
        for name, values in self.detail.items():
            if len(values) != n:
                raise ValueError(f"detail {name} has {len(values)} rows, expected {n}")

    @classmethod
    def build(cls, point, missing_reason, rule, percentiles=None, detail=None) -> ArrivalForecast:
        point = np.asarray(point, dtype=np.float32)
        reason = np.asarray(missing_reason, dtype=str)
        point = np.where(reason == "", point, np.nan).astype(np.float32)
        if percentiles is not None:
            percentiles = np.asarray(percentiles, dtype=np.float32)
            percentiles = np.where((reason == "")[:, None], percentiles, np.nan).astype(np.float32)
        return cls(point, reason, rule, percentiles, detail or {})


# ---------------------------------------------------------------------------
# Decision rules
# ---------------------------------------------------------------------------


def grouped_quantiles(values: np.ndarray, starts: np.ndarray, counts: np.ndarray,
                      qs=PERCENTILES) -> np.ndarray:
    """(groups, len(qs)) linearly interpolated quantiles of each group's sorted values."""
    out = np.empty((len(starts), len(qs)), dtype=np.float64)
    for j, q in enumerate(qs):
        pos = q * (counts - 1)
        lo = np.floor(pos).astype(np.int64)
        hi = np.minimum(lo + 1, counts - 1)
        frac = pos - lo
        out[:, j] = values[starts + lo] * (1 - frac) + values[starts + hi] * frac
    return out


def grouped_window(values: np.ndarray, starts: np.ndarray, counts: np.ndarray,
                   half: float = WINDOW_HALF_MIN) -> tuple[np.ndarray, np.ndarray]:
    """Per group, the centre of the closed interval of width 2*half holding most values.

    `values` are sorted within each group, groups laid out consecutively from
    `starts`. The best interval can always start at a value; ties go to the
    lowest such start. The centre is the midpoint of the covered values, so each
    of them lies within `half` of it. Returns (centres, share of values covered).
    """
    values = np.asarray(values, dtype=np.float64)
    if len(values) == 0:
        return np.zeros(0), np.zeros(0)
    gid = np.repeat(np.arange(len(starts)), counts)
    span = float(values.max() - values.min()) + 4 * half + 1  # groups can never overlap
    z = gid * span + (values - values.min())
    idx = np.arange(len(z))
    covered = np.searchsorted(z, z + 2 * half + _EPS, side="right") - idx
    best = np.maximum.reduceat(covered, starts)
    first = np.minimum.reduceat(np.where(covered == best[gid], idx, len(z)), starts)
    centres = (values[first] + values[first + best - 1]) / 2
    return centres, best / counts


def bin_edges(lo: float = -30.0, hi: float = 120.0, width: float = 1.0) -> np.ndarray:
    """Delay bins for distributional networks; targets beyond them fall into the end bins."""
    return np.arange(lo, hi + width / 2, width, dtype=np.float64)


def bin_index(y: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.clip(np.searchsorted(edges, y, side="right") - 1, 0, len(edges) - 2).astype(np.int64)


def distribution_point(probs: np.ndarray, edges: np.ndarray, rule: str,
                       half: float = WINDOW_HALF_MIN) -> tuple[np.ndarray, np.ndarray]:
    """(n, bins) probabilities -> (point, (n, 3) percentiles), mass uniform within a bin.

    `window` slides a 2*half-wide window along bin boundaries and takes the
    centre of the one with the most mass (ties: lowest). Percentiles interpolate
    the CDF within a bin, so they stay ordered.
    """
    probs = np.asarray(probs, dtype=np.float64)
    probs = probs / probs.sum(axis=1, keepdims=True)
    width = float(edges[1] - edges[0])
    cdf = np.cumsum(probs, axis=1)
    pct = np.empty((len(probs), 3))
    for j, q in enumerate(PERCENTILES):
        b = np.minimum((cdf < q).sum(axis=1), probs.shape[1] - 1)
        before = np.where(b > 0, cdf[np.arange(len(b)), b - 1], 0.0)
        mass = probs[np.arange(len(b)), b]
        frac = np.clip(np.where(mass > 0, (q - before) / np.where(mass > 0, mass, 1), 0.0), 0, 1)
        pct[:, j] = edges[b] + frac * width
    pct = np.maximum.accumulate(pct, axis=1)
    if rule == "median":
        return pct[:, 1], pct
    if rule != "window":
        raise ValueError(f"unknown point rule {rule!r}")
    span = int(round(2 * half / width))
    if span > probs.shape[1]:
        raise ValueError("the window is wider than the bin range")
    padded = np.concatenate([np.zeros((len(probs), 1)), cdf], axis=1)
    mass = padded[:, span:] - padded[:, :-span]
    start = np.argmax(mass, axis=1)
    return edges[start] + half, pct


# ---------------------------------------------------------------------------
# Network inputs
# ---------------------------------------------------------------------------


def numeric_stats(v: np.ndarray, scaling: str) -> tuple[float, float]:
    """(centre, scale) of one numeric column's training values, NaN ignored."""
    if scaling == "standard":
        centre, scale = float(np.nanmean(v)), float(np.nanstd(v))
    else:
        q25, centre, q75 = (float(x) for x in np.nanquantile(v, [0.25, 0.5, 0.75]))
        scale = (q75 - q25) / 1.349
    return centre, scale if scale > 0 else 1.0


def prep_layout(scaling: str, cards: list[int], numeric: tuple[str, ...], stats: dict,
                has_missing: dict[str, bool]) -> dict:
    """The JSON-able prep from per-column statistics; one definition for in-memory and streamed fits."""
    names = []
    for name in numeric:
        names.append(name)
        if has_missing[name]:
            names.append(f"{name}_missing")
        if name in CYCLIC:
            names += [f"{name}_sin", f"{name}_cos"]
    prep = {"scaling": scaling, "cards": cards, "numeric": names, "stats": stats}
    if tuple(numeric) != NUMERIC:
        prep["features_numeric"] = list(numeric)
    return prep


def fit_input_prep(encoded: dict[str, np.ndarray], train_mask: np.ndarray, vocab: dict[str, list],
                   scaling: str = "standard", numeric: tuple[str, ...] = NUMERIC) -> dict:
    """Input layout and numeric statistics from training rows only (JSON-able).

    `vocab` is the training vocabulary (`restrict_to_training_vocab`), so slot
    0 is UNKNOWN and slots 1..len(vocab) are training values. `robust` is the
    RealMLP-style variant: median/IQR scaling followed by smooth clipping.
    `numeric` is the model's numeric feature list; any other than `NUMERIC` is
    recorded in the prep so prediction builds the same layout.
    """
    if scaling not in SCALINGS:
        raise ValueError(f"unknown scaling {scaling!r}")
    if not train_mask.any():
        raise ValueError("input preparation needs training rows")
    stats, has_missing = {}, {}
    for name in numeric:
        v = encoded[name].astype(np.float64)[train_mask]
        if np.isnan(v).all():
            raise ValueError(f"{name} is missing on every training row")
        stats[name] = [0.0, 1.0] if name in SEASONAL else list(numeric_stats(v, scaling))
        has_missing[name] = bool(np.isnan(v).any())
    return prep_layout(scaling, [len(vocab[c]) + 1 for c in CATEGORICAL], tuple(numeric), stats,
                       has_missing)


def prep_numeric(prep: dict) -> tuple[str, ...]:
    return tuple(prep.get("features_numeric", NUMERIC))


def arrival_inputs(encoded: dict[str, np.ndarray], prep: dict) -> tuple[np.ndarray, np.ndarray]:
    """Encoded features -> (categorical slots int64, numerics float32), as `prep` says."""
    cards = np.asarray(prep["cards"], dtype=np.int64)
    cats = np.column_stack([encoded[c].astype(np.int64) + 1 for c in CATEGORICAL])
    cats = np.where((cats < 0) | (cats >= cards), 0, cats)
    cols = []
    for name in prep_numeric(prep):
        v = encoded[name].astype(np.float64)
        miss = np.isnan(v)
        centre, scale = prep["stats"][name]
        z = (v - centre) / scale
        if prep["scaling"] == "robust" and name not in SEASONAL:
            z = z / np.sqrt(1 + (z / 3) ** 2)
        cols.append(np.where(miss, 0.0, z))
        if f"{name}_missing" in prep["numeric"]:
            cols.append(miss.astype(np.float64))
        if name in CYCLIC:
            angle = 2 * math.pi * np.nan_to_num(v) / 1440.0
            cols += [np.sin(angle), np.cos(angle)]
    nums = np.column_stack(cols).astype(np.float32)
    if nums.shape[1] != len(prep["numeric"]):
        raise ValueError("numeric inputs do not match the prepared layout")
    return cats, nums


# ---------------------------------------------------------------------------
# The incumbent: the deployed two-sided TabM's arrival output, on the same rows
# ---------------------------------------------------------------------------


def incumbent_features(ds, rows: np.ndarray, vocab: dict[str, list]) -> dict[str, np.ndarray]:
    """The arrival dataset's rows as the v2 model saw its inputs.

    Categories are decoded to raw values and re-encoded against the v2
    manifest's vocabulary; the clock is v2's departure-preferred scheduled time,
    not the arrival clock the new candidates use.
    """
    if ds.v2_sched_local_sec is None:
        raise ValueError("the arrival dataset lacks the v2 clock column; rebuild it")
    from dbrel.ml.features import encode_categoricals

    raw = {}
    for name in CATEGORICAL:
        values = ds.meta["vocab"][name]
        table = np.array(values, dtype=object if any(isinstance(v, str) for v in values) else np.int64)
        raw[name] = table[ds.features[name][rows]]
    encoded = encode_categoricals(raw, vocab)
    for name in NUMERIC:
        encoded[name] = ds.features[name][rows]
    sched_min = np.floor((ds.v2_sched_local_sec[rows].astype(np.float64) % 86400) / 60)
    encoded["sched_min"] = sched_min.astype(np.float32)
    encoded["minutes_since_start"] = ((sched_min - ds.features["trip_start_min"][rows]) % 1440).astype(np.float32)
    return encoded


def incumbent_forecast(ds, rows: np.ndarray, artifact: Path) -> ArrivalForecast:
    from dbrel.ml.nn import TabMPredictor

    manifest = json.loads((artifact / "manifest.json").read_text())
    pred = TabMPredictor(artifact, manifest).predict(incumbent_features(ds, rows, manifest["vocab"]))["arr"]
    return ArrivalForecast.build(pred[:, 1], np.full(len(rows), ""), "median", pred,
                                 {"artifact": np.full(len(rows), artifact.name)})


# ---------------------------------------------------------------------------
# Conditional histogram with backoff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Level:
    name: str
    columns: tuple[str, ...]


# Most specific first. "sched_hour" is derived from the planned arrival clock.
DEFAULT_LEVELS = (
    Level("train_station", ("train", "station", "is_holiday")),
    Level("line_station_hour", ("category", "line", "direction", "station", "sched_hour", "is_holiday")),
    Level("category_station_hour", ("category", "station", "sched_hour", "is_holiday")),
    Level("category_hour", ("category", "sched_hour", "is_holiday")),
    Level("day_type", ("is_holiday",)),
    Level("global", ()),
)
NO_HISTORY = "no_training_history"
HISTOGRAM_FORMAT = "arrival-histogram-v1"
# Every level's columns, in the order the browser receives them per call.
HIST_COLUMNS = ("train", "station", "line", "category", "direction", "sched_hour", "is_holiday")


def histogram_inputs(encoded: dict[str, np.ndarray]) -> np.ndarray:
    """(n, len(HIST_COLUMNS)) key components per call, 0 = unknown -- what the browser is sent."""
    return np.column_stack([_component(encoded, c) for c in HIST_COLUMNS]).astype(np.int64)


def _component(encoded: dict[str, np.ndarray], column: str) -> np.ndarray:
    """Non-negative ints; 0 = unknown/missing."""
    if column == "sched_hour":
        v = encoded["sched_min"].astype(np.float64)
        return np.where(np.isnan(v), 0, np.floor(np.nan_to_num(v) / 60) % 24 + 1).astype(np.int64)
    if column == "is_holiday":
        v = encoded["is_holiday"].astype(np.float64)
        return np.where(np.isnan(v), 0, np.nan_to_num(v) + 1).astype(np.int64)
    if column in CATEGORICAL:
        return np.maximum(encoded[column].astype(np.int64) + 1, 0)
    raise ValueError(f"unknown histogram key column {column!r}")


@dataclass
class _Table:
    keys: np.ndarray      # int64, sorted
    n: np.ndarray         # int32 training arrivals per key
    window: np.ndarray    # float32 densest-window centre
    pct: np.ndarray       # float32 (keys, 3)


class ConditionalHistogram:
    """Training-only conditional arrival-delay distribution with explicit backoff.

    A call takes the first level whose key has at least `min_support` training
    arrivals (the final level accepts any support). Keys containing an unknown
    category never match, so an unseen train backs off to its line, category
    and station. Each key stores its own quantiles and densest-window point,
    fitted from individual training arrivals; the evaluated day's outcomes are
    never used.
    """

    def __init__(self, *, min_support: int = 10, point_rule: str = "window",
                 levels: tuple[Level, ...] = DEFAULT_LEVELS):
        if point_rule not in POINT_RULES:
            raise ValueError(f"unknown point rule {point_rule!r}")
        if min_support < 1 or not levels:
            raise ValueError("min_support must be >= 1 and there must be a level")
        self.min_support = min_support
        self.point_rule = point_rule
        self.levels = tuple(levels)
        self.radix: list[list[int]] = []
        self.tables: list[_Table] = []

    def _keys(self, encoded, level_index: int, rows: np.ndarray) -> np.ndarray:
        """Mixed-radix key per row; -1 where a component is unknown or out of range."""
        key = np.zeros(len(rows), dtype=np.int64)
        valid = np.ones(len(rows), dtype=bool)
        for column, radix in zip(self.levels[level_index].columns, self.radix[level_index]):
            comp = _component(encoded, column)[rows]
            valid &= (comp > 0) & (comp < radix)
            key = key * radix + np.where(valid, comp, 0)
        return np.where(valid, key, -1)

    def fit(self, encoded: dict[str, np.ndarray], y: np.ndarray, train_mask: np.ndarray):
        rows = np.flatnonzero(train_mask & np.isfinite(y))
        if not len(rows):
            raise ValueError("the histogram needs training arrivals")
        target = np.asarray(y, dtype=np.float64)[rows]
        self.radix, self.tables = [], []
        for li, level in enumerate(self.levels):
            radix = [int(_component(encoded, c)[rows].max()) + 1 for c in level.columns]
            if math.prod(radix) >= 2 ** 62:
                raise ValueError(f"level {level.name} key space is too large")
            self.radix.append(radix)
            keys = self._keys(encoded, li, np.arange(len(train_mask)))[rows]
            ok = keys >= 0
            k, v = keys[ok], target[ok]
            order = np.lexsort((v, k))
            k, v = k[order], v[order]
            uniq, starts, counts = np.unique(k, return_index=True, return_counts=True)
            window, _ = grouped_window(v, starts, counts)
            self.tables.append(_Table(
                keys=uniq, n=counts.astype(np.int32), window=window.astype(np.float32),
                pct=grouped_quantiles(v, starts, counts).astype(np.float32),
            ))
        return self

    def predict(self, encoded: dict[str, np.ndarray]) -> ArrivalForecast:
        if not self.tables:
            raise RuntimeError("fit the histogram before predicting")
        n = len(encoded["station"])
        level = np.full(n, -1, dtype=np.int64)
        support = np.zeros(n, dtype=np.int32)
        point = np.full(n, np.nan, dtype=np.float32)
        pct = np.full((n, 3), np.nan, dtype=np.float32)
        last = len(self.levels) - 1
        for li, table in enumerate(self.tables):
            todo = np.flatnonzero(level < 0)
            if not len(todo) or not len(table.keys):
                continue
            keys = self._keys(encoded, li, todo)
            pos = np.minimum(np.searchsorted(table.keys, keys), len(table.keys) - 1)
            need = 1 if li == last else self.min_support
            hit = (keys >= 0) & (table.keys[pos] == keys) & (table.n[pos] >= need)
            rows, pos = todo[hit], pos[hit]
            level[rows] = li
            support[rows] = table.n[pos]
            pct[rows] = table.pct[pos]
            point[rows] = table.window[pos] if self.point_rule == "window" else table.pct[pos, 1]
        names = np.array([lv.name for lv in self.levels] + [""])
        return ArrivalForecast.build(
            point, np.where(level < 0, NO_HISTORY, ""), self.point_rule, pct,
            {"level": names[level], "support": support},
        )

    def export(self, directory: Path) -> list[str]:
        """Write the browser asset: tables usable at predict time plus their layout.

        `histogram.bin` holds, per level, keys int64, n int32, window float32
        and p25/p50/p75 float32 x 3, each level padded to 8 bytes; `histogram.json`
        says where. Keys below `min_support` (except the last level) are dropped:
        predict never uses them, so the browser needs none of them.
        """
        extra = {c for lv in self.levels for c in lv.columns} - set(HIST_COLUMNS)
        if extra:
            raise ValueError(f"the browser is not sent {sorted(extra)}")
        last = len(self.levels) - 1
        blobs, levels, offset = [], [], 0
        for li, (level, table) in enumerate(zip(self.levels, self.tables)):
            keep = table.n >= (1 if li == last else self.min_support)
            parts = [table.keys[keep].astype("<i8"), table.n[keep].astype("<i4"),
                     table.window[keep].astype("<f4"), table.pct[keep].astype("<f4").ravel()]
            blob = b"".join(p.tobytes() for p in parts)
            blob += b"\0" * (-len(blob) % 8)
            levels.append({"name": level.name, "columns": list(level.columns),
                           "radix": self.radix[li], "count": int(keep.sum()), "offset": offset})
            blobs.append(blob)
            offset += len(blob)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "histogram.bin").write_bytes(b"".join(blobs))
        (directory / "histogram.json").write_text(json.dumps({
            "format": HISTOGRAM_FORMAT, "input_columns": list(HIST_COLUMNS),
            "min_support": self.min_support, "point_rule": self.point_rule, "levels": levels,
        }, indent=1))
        return ["histogram.json", "histogram.bin"]

    @classmethod
    def load_export(cls, directory: Path) -> ConditionalHistogram:
        meta = json.loads((directory / "histogram.json").read_text())
        if meta["format"] != HISTOGRAM_FORMAT:
            raise ValueError(f"unknown histogram format {meta['format']!r}")
        blob = (directory / "histogram.bin").read_bytes()
        levels = tuple(Level(lv["name"], tuple(lv["columns"])) for lv in meta["levels"])
        self = cls(min_support=meta["min_support"], point_rule=meta["point_rule"], levels=levels)
        for lv in meta["levels"]:
            c, o = lv["count"], lv["offset"]
            keys = np.frombuffer(blob, "<i8", c, o)
            n = np.frombuffer(blob, "<i4", c, o + 8 * c)
            window = np.frombuffer(blob, "<f4", c, o + 12 * c)
            pct = np.frombuffer(blob, "<f4", 3 * c, o + 16 * c).reshape(c, 3)
            self.radix.append(list(lv["radix"]))
            self.tables.append(_Table(keys.astype(np.int64), n.astype(np.int32),
                                      window.astype(np.float32), pct.astype(np.float32)))
        return self

    def asset_bytes(self) -> int:
        """Estimated table bytes for keys usable at predict time (8 key + 4 n + 4 x 4 floats).

        An estimate for planning; the exported browser asset is measured separately.
        """
        last = len(self.levels) - 1
        return sum(28 * int(np.count_nonzero(t.n >= (1 if li == last else self.min_support)))
                   for li, t in enumerate(self.tables))


# ---------------------------------------------------------------------------
# Deployable arrival-only artifact
# ---------------------------------------------------------------------------

ARRIVAL_KIND = "arrival-net"
ARRIVAL_MODEL_FILE = "model.onnx"
_BROWSER_FIELDS = ("kind", "output", "point_rule", "edges", "sides", "window_half_min")


class ArrivalNetPredictor:
    """ONNX inference for an `arrival-net` artifact: arrival point + genuine p25/p75, no departure."""

    BATCH = 4096

    def __init__(self, artifact: Path, manifest: dict, session=None, *, inference_threads: int | None = None):
        if manifest.get("kind") != ARRIVAL_KIND:
            raise ValueError(f"not an {ARRIVAL_KIND} artifact")
        self.manifest = manifest
        self.edges = np.asarray(manifest["edges"], dtype=np.float64) if manifest["edges"] else None
        if session is None:
            import onnxruntime

            options = {}
            if inference_threads is not None:
                if inference_threads < 1:
                    raise ValueError("inference_threads must be positive")
                config = onnxruntime.SessionOptions()
                config.intra_op_num_threads = inference_threads
                config.inter_op_num_threads = 1
                options["sess_options"] = config
            session = onnxruntime.InferenceSession(str(artifact / manifest["model_file"]),
                                                   providers=["CPUExecutionProvider"], **options)
        self.session = session

    def predict(self, encoded: dict[str, np.ndarray]) -> ArrivalForecast:
        m = self.manifest
        cats, nums = arrival_inputs(encoded, m["prep"])
        point, pct = np.full(len(cats), np.nan), np.full((len(cats), 3), np.nan)
        for s in range(0, len(cats), self.BATCH):
            raw = self.session.run([m["output"]], {"cat": cats[s:s + self.BATCH],
                                                   "num": nums[s:s + self.BATCH]})[0].astype(np.float64)
            if m["output"] == "probs":
                point[s:s + len(raw)], pct[s:s + len(raw)] = distribution_point(raw, self.edges, m["point_rule"])
            else:
                pct[s:s + len(raw)], point[s:s + len(raw)] = raw, raw[:, 1]
        return ArrivalForecast.build(point, np.full(len(cats), ""), m["point_rule"], pct)


def browser_meta(manifest: dict) -> dict:
    """What the browser needs to turn the ONNX output into a forecast -- nothing else."""
    if manifest.get("kind") != ARRIVAL_KIND:
        return {"kind": manifest.get("kind", "tabm"), "output": "quantiles",
                "sides": manifest.get("sides", ["dep", "arr"]), "point_rule": "median"}
    return {k: manifest[k] for k in _BROWSER_FIELDS}


# What each artifact role may weigh (model files + manifest, i.e. what a browser would download).
# A comparison artifact is never served: it lives outside the served directory and `promote`
# refuses it. Manifests without a role are deployable.
ROLE_BUDGETS = {"deployable": 35_000_000, "comparison": 100_000_000}
COMPARISON_DIR_NAME = "v2-comparison"
OUTPUTS = ("probs", "quantiles")


def role_models_dir(role: str, models_dir: Path) -> Path:
    """Where an artifact of `role` is written: the served directory, or its comparison sibling."""
    if role not in ROLE_BUDGETS:
        raise ValueError(f"unknown artifact role {role!r}")
    return models_dir if role == "deployable" else models_dir.parent / COMPARISON_DIR_NAME


def save_arrival_artifact(models_dir: Path, manifest: dict, files: dict[str, Path], stamp: str, *,
                          role: str | None = None, served_dir: Path | None = None) -> Path:
    """Write a new, complete artifact directory. Never moves `CURRENT` (see `promote`).

    With a `role`, the manifest records it and the artifact must fit that
    role's budget; a `comparison` artifact is refused inside `served_dir`
    (default: `ML_MODELS_DIR`).
    """
    import shutil

    from dbrel.ml.nn import MANIFEST_NAME

    missing = {"prep", "vocab", "edges", "point_rule", "output"} - manifest.keys()
    if missing or ARRIVAL_MODEL_FILE not in files:
        raise ValueError(f"incomplete arrival artifact: missing {sorted(missing) or ARRIVAL_MODEL_FILE}")
    if any(n in NOTICE_RATES for n in manifest["prep"].get("features_numeric", ())) \
            and "notice_rates" not in manifest:
        raise ValueError("incomplete arrival artifact: its inputs include notice rates but it records no "
                         "notice_rates (dates, weight, fingerprint)")
    if manifest["output"] not in OUTPUTS or (manifest["output"] == "probs") != (manifest["edges"] is not None):
        raise ValueError("output must be 'probs' with bin edges or 'quantiles' with edges null")
    if role is not None:
        if role not in ROLE_BUDGETS:
            raise ValueError(f"unknown artifact role {role!r}")
        if served_dir is None:
            from dbrel.config import ML_MODELS_DIR as served_dir
        if role == "comparison" and Path(models_dir).resolve() == Path(served_dir).resolve():
            raise ValueError(f"a comparison artifact must not be written into the served {served_dir}")
    final = models_dir / stamp
    if final.exists():
        raise FileExistsError(f"{final} already exists")
    partial = models_dir / f".{stamp}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    for name, source in files.items():
        shutil.copyfile(source, partial / name)
    asset = sum((partial / n).stat().st_size for n in files)
    full = {**manifest, "kind": ARRIVAL_KIND, "model_file": ARRIVAL_MODEL_FILE, "sides": ["arr"],
            "window_half_min": WINDOW_HALF_MIN, "asset_bytes": asset}
    if role is not None:
        full.update(role=role, budget_bytes=ROLE_BUDGETS[role], download_bytes=0)
        for _ in range(2):  # the manifest counts itself; its length settles on the second pass
            full["download_bytes"] = asset + len(json.dumps(full, indent=1).encode())
        if full["download_bytes"] > ROLE_BUDGETS[role]:
            shutil.rmtree(partial)
            raise ValueError(f"{role} artifact is {full['download_bytes']:,} bytes, over its "
                             f"{ROLE_BUDGETS[role]:,}-byte budget")
    (partial / MANIFEST_NAME).write_text(json.dumps(full, indent=1))
    partial.rename(final)
    return final


def artifact_role(manifest: dict) -> str:
    return manifest.get("role", "deployable")


def promote(models_dir: Path, stamp: str) -> str | None:
    """Atomically point `CURRENT` at a complete artifact; returns the previous stamp for rollback.

    Comparison artifacts are refused wherever they are found.
    """
    from dbrel.ml.nn import CURRENT_NAME, MANIFEST_NAME

    if not (models_dir / stamp / MANIFEST_NAME).exists():
        raise FileNotFoundError(f"{models_dir / stamp} is not a complete artifact")
    role = artifact_role(json.loads((models_dir / stamp / MANIFEST_NAME).read_text()))
    if role != "deployable":
        raise ValueError(f"{stamp} is a {role} artifact, which is never served; CURRENT is unchanged")
    pointer = models_dir / CURRENT_NAME
    previous = pointer.read_text().strip() if pointer.exists() else None
    tmp = models_dir / (CURRENT_NAME + ".tmp")
    tmp.write_text(stamp + "\n")
    tmp.replace(pointer)
    return previous
