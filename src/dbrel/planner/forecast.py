"""Batch arrival forecasts, shared by all searches in a request."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from threading import Lock
from typing import NamedTuple, Protocol

import numpy as np
from sqlalchemy import select

from dbrel.direction import direction_of
from dbrel.ml.features import UNKNOWN_DIRECTION

from dbrel.ml.predict import DelayModel
from dbrel.ml.timetable import ScheduledCall, _variant_trip_start
from dbrel.models import Station, Train
from .constants import FORECAST_CACHE_RUNS, MAX_DEP_DELAY_S, NO_TIME

RunKey = tuple[int, date]
# Timetable objects provide generation-safe identity for local variant indices.
_FORECAST_CACHE = OrderedDict()
_FORECAST_LOCK = Lock()
_MODEL_LOCK = Lock()
_MODELS = {}
_DIRECTIONS = {}


class ArrivalForecaster(Protocol):
    stamp: str

    def forecast(self, runs: list[RunKey]) -> dict[RunKey, np.ndarray]: ...


class _Row(NamedTuple):
    stop_sequence: int
    planned_arrival_sec: int | None
    planned_departure_sec: int | None


def delay_s(p50):
    """Clamp early forecasts to zero; missing forecasts use the schedule."""
    return 0 if np.isnan(p50) else max(0, round(float(p50) * 60))


def expected_departure(tt, arr, anchor, base, pos):
    """Carry at most thirty minutes of arrival delay into a connecting departure."""
    carried = 0
    if pos > 0 and tt.stop_arr[base + pos] != NO_TIME:
        carried = min(MAX_DEP_DELAY_S, delay_s(arr[pos, 1]))
    return int(anchor + tt.stop_dep[base + pos] + carried)


class FixedForecaster:
    def __init__(self, tt, delays, default=(0.0, 0.0, 0.0), stamp="test"):
        self.tt, self.delays, self.default, self.stamp = tt, delays, default, stamp
        self.calls = self.runs_forecast = 0
        self.keys = []

    def forecast(self, runs):
        self.calls += 1
        self.runs_forecast += len(runs)
        self.keys.extend(runs)
        result = {}
        for key in runs:
            v, _ = key
            arr = np.full((int(self.tt.var_len[v]), 3), np.nan, dtype=np.float32)
            base = int(self.tt.var_first[v])
            for p in range(int(self.tt.var_len[v])):
                if self.tt.stop_arr[base + p] != NO_TIME:
                    station = int(self.tt.station_id[self.tt.stop_si[base + p]])
                    arr[p] = self.delays.get(
                        (int(self.tt.var_train[v]), station), self.default
                    )
            result[key] = arr
        return result


class _DirectionRoute(NamedTuple):
    train_id: int
    names: tuple[str, ...]
    stations: frozenset[str]
    raw_length: int


class _PlannerDirectionCache:
    """Cache scalar corridor routes once per category, never retaining ORM rows."""

    def __init__(self):
        from dbrel.web.queries import _ModelDirectionCache

        self.regional = _ModelDirectionCache()
        self.categories = {}
        self.directions = {}

    def get(self, session, train):
        if train.line_id is not None:
            return self.regional.get(session, train)
        if train.id in self.directions:
            return self.directions[train.id]
        route = tuple(part for part in (train.route_path or "").split("|") if part)
        if len(route) < 2:
            self.directions[train.id] = UNKNOWN_DIRECTION
            return UNKNOWN_DIRECTION
        if train.category not in self.categories:
            rows = session.execute(
                select(Train.id, Train.route_path).where(
                    Train.line_id.is_(None), Train.category == train.category
                )
            )
            snapshots = []
            # Category-local pooling releases duplicate station strings without
            # extending their lifetime beyond this timetable/model cache.
            station_names = {}
            route_tokens = {}
            for identifier, path in rows:
                raw = (path or "").split("|")
                names = tuple(station_names.setdefault(part, part) for part in raw if part)
                if len(names) >= 2:
                    shared = route_tokens.get(names)
                    if shared is None:
                        shared = (names, frozenset(names))
                        route_tokens[names] = shared
                    snapshots.append(
                        _DirectionRoute(identifier, shared[0], shared[1], len(raw))
                    )
            # The first corridor member is its longest axis (lowest id on ties).
            self.categories[train.category] = tuple(
                sorted(
                    snapshots,
                    key=lambda row: (row.raw_length, -row.train_id),
                    reverse=True,
                )
            )
        on_route = frozenset(route)
        members = (
            candidate
            for candidate in self.categories[train.category]
            if (route[0] in candidate.stations and route[-1] in candidate.stations)
            or (candidate.names[0] in on_route and candidate.names[-1] in on_route)
        )
        axis = next(members, None)
        names = route if axis is None else axis.names
        result = direction_of(train, {name: pos for pos, name in enumerate(names)})
        self.directions[train.id] = result
        return result


class ModelForecaster:
    def __init__(self, tt, session, artifact_dir, stamp):
        self.tt, self.session, self.stamp = tt, session, stamp
        self.trains, self.stations = {}, {}
        with _MODEL_LOCK:
            if stamp not in _MODELS:
                _MODELS.clear()
                _DIRECTIONS.clear()
                # Small planner batches run on a shared single-CPU server; a
                # host-sized ORT pool adds synchronization work to every batch.
                _MODELS[stamp] = DelayModel(artifact_dir, inference_threads=1)
            direction_key = (tt, stamp)
            if direction_key not in _DIRECTIONS:
                _DIRECTIONS[direction_key] = _PlannerDirectionCache()
            self.model, self.direction = _MODELS[stamp], _DIRECTIONS[direction_key]

    def forecast(self, runs):
        result, missing = {}, []
        with _FORECAST_LOCK:
            for key in dict.fromkeys(runs):
                cache_key = (self.tt, self.stamp, *key)
                if cache_key in _FORECAST_CACHE:
                    result[key] = _FORECAST_CACHE[cache_key]
                    _FORECAST_CACHE.move_to_end(cache_key)
                else:
                    missing.append(key)
        calls, locations = [], []
        for key in missing:
            v, day = key
            base, n = int(self.tt.var_first[v]), int(self.tt.var_len[v])
            train_id = int(self.tt.var_train[v])
            if train_id not in self.trains:
                self.trains[train_id] = self.session.get(Train, train_id)
            train = self.trains[train_id]
            rows = [
                _Row(
                    int(self.tt.stop_seq[base + p]),
                    None
                    if self.tt.stop_arr[base + p] == NO_TIME
                    else int(self.tt.stop_arr[base + p]),
                    None
                    if self.tt.stop_dep[base + p] == NO_TIME
                    else int(self.tt.stop_dep[base + p]),
                )
                for p in range(n)
            ]
            start = _variant_trip_start(train, rows)
            result[key] = np.full((n, 3), np.nan, dtype=np.float32)
            for p, row in enumerate(rows[: int(self.tt.var_cut[v])]):
                if row.planned_arrival_sec is None:
                    continue
                station_id = int(self.tt.station_id[self.tt.stop_si[base + p]])
                if station_id not in self.stations:
                    self.stations[station_id] = self.session.get(Station, station_id)
                calls.append(
                    ScheduledCall(
                        train,
                        self.stations[station_id],
                        day,
                        row.planned_arrival_sec,
                        row.planned_departure_sec,
                        p,
                        n,
                        start,
                    )
                )
                locations.append((key, p))
        if calls:
            predictions = self.model.predict_calls(
                self.session, calls, direction=self.direction.get
            )
            for (key, p), pred in zip(locations, predictions, strict=True):
                if pred.arr is not None:
                    result[key][p] = pred.arr
        with _FORECAST_LOCK:
            for key in missing:
                _FORECAST_CACHE[(self.tt, self.stamp, *key)] = result[key]
                while len(_FORECAST_CACHE) > FORECAST_CACHE_RUNS:
                    _FORECAST_CACHE.popitem(last=False)
        return result
