"""Running the model at request time, against exactly the calls being shown.

The alternative was tried first: materialise every known call into
train_stop_prediction and read it back, the same way train_stop_average works.
That got reverted — it scored tens of thousands of calls nobody asked about to
answer a question that only ever concerns a handful of them (the 2-3 trips the
browse widget shows, or one train's own stops), and it meant a stale table that
only updated when someone remembered to rerun the CLI.

This module takes the opposite trade: the web process now depends on the `ml`
extra to get a forecast at all, and each prediction costs LightGBM inference
instead of a row read, but the model runs on the arguments a person actually
chose — a train, a station, a day type, a time — the moment they choose them.

Degrades to None rather than raising: no lightgbm installed, no activated model,
or an unknown categorical value all just mean "no forecast for this one", the
same way a cold-start stop mid-average has always looked. That keeps `ml` an
optional extra for the web process too — a `web`-only install just never shows
the forecast line.

Model and vocabularies are loaded once per process and cached; a freshly trained
model is picked up on the next server restart, not mid-process, which matches
the existing manual-restart workflow for template/view changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dbrel.ml import artifacts, conformal
from dbrel.ml.features import Dataset, build_row, encode

_UNLOADED = object()
_cache: Any = _UNLOADED


@dataclass(slots=True)
class LivePrediction:
    """Field names match what web/queries._prediction() expects to read."""

    p50_dep_delay_s: float
    conformal_lo_s: float
    conformal_hi_s: float
    model_id: str


def _loaded():
    global _cache
    if _cache is _UNLOADED:
        try:
            import lightgbm  # noqa: F401
        except ImportError:
            _cache = None
            return _cache
        found = artifacts.latest()
        if found is None:
            _cache = None
            return _cache
        directory, manifest = found
        boosters = artifacts.load_boosters(directory, manifest.quantiles)
        _cache = (manifest, boosters)
    return _cache


def reset_cache() -> None:
    """Forces the next call to reload from disk. Only tests need this."""
    global _cache
    _cache = _UNLOADED


def _row_of(request: dict) -> list:
    train, station = request["train"], request["station"]
    return build_row(
        day_type=request["day_type"],
        train_id=train.id,
        station_id=station.id,
        sched_sec=request["sched_sec"],
        line_id=train.line_id,
        category=train.category,
        operator=train.operator,
        segment=train.segment,
        platform=request.get("platform"),
        stop_sequence=request.get("stop_sequence"),
        n_stops=train.n_stops,
        first_departure_sec=train.first_departure_sec,
        lat=station.lat,
        lon=station.lon,
    )


def predict_many(requests: list[dict]) -> list[LivePrediction | None]:
    """The model's answer for however many calls one page is about to show.

    One booster call for the whole list rather than one per request — a line
    page listing every run still means a single round of inference, not one per
    row. Each dict needs day_type, train, station and sched_sec, and may add
    platform and stop_sequence.
    """
    if not requests:
        return []
    loaded = _loaded()
    if loaded is None:
        return [None] * len(requests)
    manifest, boosters = loaded

    matrix = encode(Dataset(rows=[_row_of(r) for r in requests]), manifest.vocabularies)
    lo_q, mid_q, hi_q = manifest.quantiles
    lo_pred = list(boosters[lo_q].predict(matrix))
    p50 = list(boosters[mid_q].predict(matrix))
    hi_pred = list(boosters[hi_q].predict(matrix))
    lo, hi = conformal.apply(lo_pred, hi_pred, manifest.conformal_adjustment)
    return [
        LivePrediction(
            p50_dep_delay_s=float(p50[i]),
            conformal_lo_s=float(lo[i]),
            conformal_hi_s=float(hi[i]),
            model_id=manifest.model_id,
        )
        for i in range(len(requests))
    ]


def predict_stop(
    *,
    day_type: str,
    train,
    station,
    sched_sec: int,
    platform: str | None = None,
    stop_sequence: int | None = None,
) -> LivePrediction | None:
    """The model's answer for one call, or None when it has nothing to say.

    `train` and `station` are ORM rows the caller already has in hand from
    building the page — this does no database access of its own.
    """
    return predict_many([{
        "day_type": day_type,
        "train": train,
        "station": station,
        "sched_sec": sched_sec,
        "platform": platform,
        "stop_sequence": stop_sequence,
    }])[0]
