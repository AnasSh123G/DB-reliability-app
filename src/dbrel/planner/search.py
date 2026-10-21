"""Lazy, batched A* over forecast arrival times."""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from heapq import heappop, heappush
from itertools import count
from time import monotonic
import numpy as np
from dbrel.ingest.timeutil import local_date, service_date_anchor
from .constants import (
    NO_TIME,
    MIN_TRANSFER_S,
    MAX_WAIT_START_S,
    MAX_WAIT_TRANSFER_S,
    MAX_DEP_DELAY_S,
    MAX_JOURNEY_S,
    MAX_LEGS,
    UNREACHABLE_S,
    MAX_RUNS_PER_SEARCH,
    SEARCH_SECONDS,
    REQUEST_SECONDS,
    FORECAST_BATCH_WINDOW_S,
    FORECAST_BATCH_MAX_RUNS,
)
from .forecast import RunKey, delay_s
from .profile import day_key
from .timetable import Departure

_NO_BOUND = np.iinfo(np.int64).max


@dataclass(frozen=True, slots=True)
class Ban:
    line_key: str
    board_si: int


@dataclass(frozen=True, slots=True)
class LegRef:
    variant: int
    service_date: date
    board_pos: int
    alight_pos: int


StateKey = tuple[int, int, int, int | None]  # station, legs, arrival, first departure


@dataclass(frozen=True, slots=True)
class Label:
    arrival: int
    legs: int
    leg: LegRef | None
    prev_state: StateKey | None
    first_departure: int | None


@dataclass(frozen=True, slots=True)
class RunScan:
    key: RunKey
    board_pos: int
    board_si: int
    legs_before: int
    station_arrival: int
    at_source: bool
    prev_state: StateKey
    first_departure: int


@dataclass(slots=True)
class SearchResult:
    legs: list[LegRef] | None
    hit_limit: bool
    runs_forecast: int


class Budget:
    def __init__(
        self,
        *,
        max_runs_per_search=MAX_RUNS_PER_SEARCH,
        search_seconds=SEARCH_SECONDS,
        request_seconds=REQUEST_SECONDS,
    ):
        self.max_runs_per_search = max_runs_per_search
        self.search_seconds, self.request_seconds = search_seconds, request_seconds
        self.request_start = monotonic()
        self.total_runs = 0
        self.start_search()

    def start_search(self):
        self.search_start, self.runs = monotonic(), 0

    def charge(self, n_runs):
        self.runs += n_runs
        self.total_runs += n_runs

    def exhausted(self):
        return (
            self.runs >= self.max_runs_per_search
            or monotonic() - self.search_start >= self.search_seconds
            or self.request_exhausted()
        )

    def request_exhausted(self):
        return monotonic() - self.request_start >= self.request_seconds


def heuristic(tt, target_si):
    """Seconds each station is from the target at the very least, by the scheduled hops."""
    return tt.travel_floor(target_si)


def lower_bound(tt, departure, end, h):
    base = int(tt.var_first[departure.variant])
    anchor = service_date_anchor(departure.service_date)
    values = [
        anchor + int(tt.stop_arr[base + j]) + int(h[tt.stop_si[base + j]])
        for j in range(departure.position + 1, int(end))
        if tt.stop_arr[base + j] != NO_TIME and h[tt.stop_si[base + j]] < UNREACHABLE_S
    ]
    return min(values) if values else None


class _SearchCache:
    """Reuse schedule bounds and exact service-day departure indexes for one search."""

    def __init__(self, tt, h):
        self.tt, self.h = tt, h
        self._suffix = {}
        self._anchors = {}
        self._eligible = {}
        self._instances = {}
        self._delays = {}

    def delays(self, key, arr):
        values = self._delays.get(key)
        if values is None:
            # Reuse the scalar rule exactly, including Python rounding of float32 inputs.
            values = tuple(delay_s(value) for value in arr[:, 1])
            self._delays[key] = values
        return values

    def expected_departure(self, key, arr, anchor, base, pos):
        carried = 0
        if pos > 0 and self.tt.stop_arr[base + pos] != NO_TIME:
            carried = min(MAX_DEP_DELAY_S, self.delays(key, arr)[pos])
        return int(anchor + self.tt.stop_dep[base + pos] + carried)

    def anchor(self, day):
        if day not in self._anchors:
            self._anchors[day] = service_date_anchor(day)
        return self._anchors[day]

    def lower_bound(self, departure, end):
        key = (departure.variant, int(end))
        if key not in self._suffix:
            base = int(self.tt.var_first[departure.variant])
            arr = self.tt.stop_arr[base : base + end]
            stations = self.tt.stop_si[base : base + end]
            values = np.where(
                (arr != NO_TIME) & (self.h[stations] < UNREACHABLE_S),
                arr.astype(np.int64) + self.h[stations],
                _NO_BOUND,
            )
            self._suffix[key] = np.minimum.accumulate(values[::-1])[::-1]
        suffix = self._suffix[key]
        pos = departure.position + 1
        if pos >= len(suffix) or suffix[pos] == _NO_BOUND:
            return None
        return self.anchor(departure.service_date) + int(suffix[pos])

    def _station_day(self, si, day):
        k = day_key(day, self.tt.station_state[si], self.tt.holiday_set)
        key = (si, k)
        eligible = self._eligible.get(key)
        if eligible is None:
            a, b = self.tt.dep_start[si : si + 2]
            rows = self.tt.dep_rows[a:b]
            variants = self.tt.stop_variant[rows]
            rows = rows[((self.tt.var_days[variants] >> k) & 1) != 0]
            eligible = rows, self.tt.stop_dep[rows]
            self._eligible[key] = eligible
        objects = self._instances.get((si, day))
        if objects is None:
            objects = [None] * len(eligible[0])
            self._instances[(si, day)] = objects
        return *eligible, objects

    def departures(self, si, lo, hi):
        lo_day = local_date(lo)
        result = []
        for day in dict.fromkeys((lo_day - timedelta(days=1), lo_day, local_date(hi))):
            anchor = self.anchor(day)
            rows, times, objects = self._station_day(si, day)
            first = times.searchsorted(lo - anchor, side="left")
            last = times.searchsorted(hi - anchor, side="right")
            for pos in range(first, last):
                departure = objects[pos]
                if departure is None:
                    row = rows[pos]
                    variant = int(self.tt.stop_variant[row])
                    departure = Departure(
                        variant,
                        day,
                        int(row - self.tt.var_first[variant]),
                        anchor + int(times[pos]),
                    )
                    objects[pos] = departure
                result.append(departure)
        return sorted(result, key=lambda dep: (dep.dep_epoch, dep.variant))


def reconstruct(labels, target):
    result = []
    while labels[target].leg is not None:
        result.append(labels[target].leg)
        target = labels[target].prev_state
    return result[::-1]


class _PendingRuns:
    """Keep the easiest feasible arrival for each otherwise identical boarding scan."""

    def __init__(self):
        self._best = {}

    @staticmethod
    def _key(scan):
        return scan.key, scan.board_pos, scan.legs_before, scan.first_departure

    def offer(self, scan):
        key = self._key(scan)
        previous = self._best.get(key)
        if previous is not None and previous.station_arrival <= scan.station_arrival:
            return False
        self._best[key] = scan
        return True

    def active(self, scan):
        return self._best.get(self._key(scan)) is scan

    def discard(self, scan):
        self._best.pop(self._key(scan), None)


def _frontier_key(state):
    return state[0], state[2], state[1] == 0


def _frontier_rank(state):
    return state[1], -(state[3] if state[3] is not None else state[2])


def earliest_arrival(
    tt, fc, source_si, target_si, depart_epoch, bans, budget, cache, depart_until=None
):
    """Earliest forecast arrival; `depart_until` caps the first train's scheduled departure."""
    budget.start_search()
    h = heuristic(tt, target_si)
    if h[source_si] >= UNREACHABLE_S:
        return SearchResult(None, False, 0)
    schedule = _SearchCache(tt, h)
    seq = count()
    # Later arrivals have later wait windows; fewer-leg arrivals retain more rides.
    start = (source_si, 0, depart_epoch, None)
    labels = {start: Label(depart_epoch, 0, None, None, None)}
    # At identical arrivals, fewer rides wins; equal ride counts prefer a later start.
    # The initial state has a different boarding window from a return to the source.
    frontier = {_frontier_key(start): (0, -depart_epoch)}
    heap = [(depart_epoch + int(h[source_si]), 0, -depart_epoch, next(seq), 0, start)]
    settled = set()
    boarded = {}
    pending = _PendingRuns()
    banned_lines = {
        ban.board_si: frozenset(
            item.line_key for item in bans if item.board_si == ban.board_si
        )
        for ban in bans
    }
    horizon = depart_epoch + MAX_JOURNEY_S
    best_target = None
    while heap:
        # Equal lower bounds may still improve the leg count or departure tie.
        if best_target is not None and heap[0][0] > best_target[2]:
            break
        if budget.exhausted():
            legs = None if best_target is None else reconstruct(labels, best_target)
            return SearchResult(legs, True, budget.runs)
        f, legs, _, _, kind, payload = heappop(heap)
        if kind == 0:
            state = payload
            si, arrival = state[0], state[2]
            lab = labels[state]
            if (
                state in settled
                or _frontier_rank(state) != frontier[_frontier_key(state)]
            ):
                continue
            settled.add(state)
            if si == target_si:
                if best_target is None or (arrival, legs, -state[3]) < (
                    best_target[2],
                    best_target[1],
                    -best_target[3],
                ):
                    best_target = state
                continue
            if lab.legs >= MAX_LEGS:
                continue
            at_source = lab.legs == 0
            lo = arrival if at_source else arrival + MIN_TRANSFER_S - MAX_DEP_DELAY_S
            hi = arrival + (MAX_WAIT_START_S if at_source else MAX_WAIT_TRANSFER_S)
            if at_source and depart_until is not None:
                hi = min(hi, depart_until)
            banned_here = banned_lines.get(si, ())
            for dep in schedule.departures(si, lo, hi):
                if tt.var_line_key[dep.variant] in banned_here:
                    continue
                first_dep = dep.dep_epoch if at_source else state[3]
                claim = boarded.get(
                    ((dep.variant, dep.service_date), lab.legs, first_dep)
                )
                if claim is not None and dep.position >= claim:
                    continue
                lb = schedule.lower_bound(dep, tt.var_cut[dep.variant])
                if lb is not None:
                    scan = RunScan(
                        (dep.variant, dep.service_date),
                        dep.position,
                        si,
                        lab.legs,
                        arrival,
                        at_source,
                        state,
                        first_dep,
                    )
                    if pending.offer(scan):
                        heappush(
                            heap, (lb, lab.legs + 1, -first_dep, next(seq), 1, scan)
                        )
        else:
            scan = payload
            if (
                not pending.active(scan)
                or _frontier_rank(scan.prev_state)
                != frontier[_frontier_key(scan.prev_state)]
            ):
                continue
            if scan.key not in cache:
                batch = [scan.key]
                for item in heap:
                    if (
                        item[4] == 1
                        and item[0] <= f + FORECAST_BATCH_WINDOW_S
                        and pending.active(item[5])
                        and _frontier_rank(item[5].prev_state)
                        == frontier[_frontier_key(item[5].prev_state)]
                    ):
                        key = item[5].key
                        if key not in cache and key not in batch:
                            batch.append(key)
                            if len(batch) >= FORECAST_BATCH_MAX_RUNS:
                                break
                cache.update(fc.forecast(batch))
                budget.charge(len(batch))
                if budget.exhausted():
                    legs = (
                        None
                        if best_target is None
                        else reconstruct(labels, best_target)
                    )
                    return SearchResult(legs, True, budget.runs)
            arr = cache[scan.key]
            v, day = scan.key
            anchor = schedule.anchor(day)
            base = int(tt.var_first[v])
            if (
                not scan.at_source
                and schedule.expected_departure(
                    scan.key, arr, anchor, base, scan.board_pos
                )
                < scan.station_arrival + MIN_TRANSFER_S
            ):
                continue
            # Only prune scans with identical remaining rides and departure tie.
            board_key = (scan.key, scan.legs_before, scan.first_departure)
            prev = boarded.get(board_key)
            if prev is not None and scan.board_pos >= prev:
                pending.discard(scan)
                continue
            boarded[board_key] = scan.board_pos
            pending.discard(scan)
            end = int(tt.var_cut[v]) if prev is None else prev + 1
            delays = schedule.delays(scan.key, arr)
            for j in range(scan.board_pos + 1, end):
                a = tt.stop_arr[base + j]
                if a == NO_TIME:
                    continue
                t = anchor + int(a) + delays[j]
                sj = int(tt.stop_si[base + j])
                if t > horizon or h[sj] >= UNREACHABLE_S:
                    continue
                n = scan.legs_before + 1
                state = (sj, n, t, scan.first_departure)
                rank = _frontier_rank(state)
                previous_rank = frontier.get(_frontier_key(state))
                if previous_rank is not None and rank >= previous_rank:
                    continue
                if state not in labels:
                    frontier[_frontier_key(state)] = rank
                    labels[state] = Label(
                        t,
                        n,
                        LegRef(v, day, scan.board_pos, j),
                        scan.prev_state,
                        scan.first_departure,
                    )
                    heappush(
                        heap,
                        (t + int(h[sj]), n, -scan.first_departure, next(seq), 0, state),
                    )
    legs = None if best_target is None else reconstruct(labels, best_target)
    return SearchResult(legs, False, budget.runs)
