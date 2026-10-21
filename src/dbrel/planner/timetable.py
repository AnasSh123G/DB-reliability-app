"""Compact immutable-by-convention timetable and station departure index."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import date, timedelta
from heapq import heappop, heappush
import logging
from threading import Lock
from time import monotonic

import numpy as np
from sqlalchemy import text

from dbrel.holidays_de import load_holiday_dates
from dbrel.ingest.timeutil import local_date, service_date_anchor
from dbrel.planner.constants import MAX_STEP_S, NO_TIME, UNREACHABLE_S
from dbrel.planner.profile import chosen_variants, day_key, load_run_profile

log = logging.getLogger(__name__)
_FLOOR_LOCK = Lock()
RunKey = tuple[int, date]


@dataclass(frozen=True, slots=True)
class Departure:
    variant: int
    service_date: date
    position: int
    dep_epoch: int


class PlannerTimetable:
    """Arrays use compact station and variant indices, retaining original IDs."""

    @classmethod
    def from_columns(
        cls,
        *,
        stations,
        train_id,
        path_code,
        path_values,
        stop_sequence,
        station_id,
        arr,
        dep,
        chosen,
        line_key_of_train,
        holiday_set,
        first_date,
        last_date,
    ):
        tt = cls()
        tt.station_id = np.array([s[0] for s in stations], dtype=np.int64)
        tt.station_name = [s[1] for s in stations]
        tt.station_lat = np.array([s[2] for s in stations], dtype=np.float64)
        tt.station_lon = np.array([s[3] for s in stations], dtype=np.float64)
        tt.station_state = [s[4] for s in stations]
        tt.si_of_id = {int(s): i for i, s in enumerate(tt.station_id)}
        variants, trains, paths = {}, [], []
        variant_buffer, station_buffer = array("i"), array("i")
        for tr, pc, station in zip(train_id, path_code, station_id, strict=True):
            key = (int(tr), path_values[int(pc)])
            if key not in variants:
                variants[key] = len(trains)
                trains.append(key[0])
                paths.append(key[1])
            variant_buffer.append(variants[key])
            station_buffer.append(tt.si_of_id[int(station)])
        variant = np.frombuffer(variant_buffer, dtype=np.int32)
        order = np.lexsort((stop_sequence, variant))
        tt.stop_variant = variant[order]
        tt.stop_si = np.frombuffer(station_buffer, dtype=np.int32)[order]
        tt.stop_seq = np.asarray(stop_sequence, dtype=np.int32)[order]
        tt.stop_arr = np.asarray(arr, dtype=np.int32)[order]
        tt.stop_dep = np.asarray(dep, dtype=np.int32)[order]
        tt.var_train = np.array(trains, dtype=np.int64)
        tt.var_path = paths
        tt.var_len = np.bincount(tt.stop_variant, minlength=len(trains)).astype(
            np.int32
        )
        tt.var_first = (
            np.concatenate(([0], np.cumsum(tt.var_len)[:-1])).astype(np.int32)
            if trains
            else np.array([], dtype=np.int32)
        )
        tt.var_cut = tt._cuts()
        tt.var_days = np.array(
            [
                sum(1 << k for k, p in enumerate(chosen[tr]) if p == path)
                for tr, path in zip(trains, paths, strict=True)
            ],
            dtype=np.uint8,
        )
        tt.var_line_key = [line_key_of_train[tr] for tr in trains]
        position = np.arange(len(order)) - tt.var_first[tt.stop_variant]
        usable = position < tt.var_cut[tt.stop_variant]
        tt.si_has_arrival = np.zeros(len(stations), dtype=bool)
        tt.si_has_arrival[tt.stop_si[usable & (tt.stop_arr != NO_TIME)]] = True
        mask = (tt.stop_dep != NO_TIME) & (position < tt.var_cut[tt.stop_variant] - 1)
        rows = np.nonzero(mask)[0]
        tt.dep_rows = rows[np.lexsort((tt.stop_dep[rows], tt.stop_si[rows]))].astype(
            np.int32
        )
        tt.dep_time = tt.stop_dep[tt.dep_rows]
        tt.dep_start = np.searchsorted(
            tt.stop_si[tt.dep_rows], np.arange(len(stations) + 1)
        ).astype(np.int64)
        tt.holiday_set, tt.first_date, tt.last_date = holiday_set, first_date, last_date
        return tt

    def _cuts(self):
        cuts = self.var_len.copy()
        for v, (first, length) in enumerate(
            zip(self.var_first, self.var_len, strict=True)
        ):
            last = None
            for position in range(int(length)):
                row = int(first) + position
                arr, dep = int(self.stop_arr[row]), int(self.stop_dep[row])
                if arr == NO_TIME and dep == NO_TIME:
                    continue
                t_in = arr if arr != NO_TIME else dep
                if last is not None and (t_in < last or t_in - last > MAX_STEP_S):
                    cuts[v] = position
                    break
                last = dep if dep != NO_TIME else arr
        return cuts

    def nbytes(self):
        return sum(
            value.nbytes
            for value in vars(self).values()
            if isinstance(value, np.ndarray)
        )

    def weekly_calls(self):
        """How many times a week trains call at each station, by station id.

        Every stop of a variant counts once for each weekday it runs on, so a
        station's figure is how busy it is in an ordinary week.
        """
        if getattr(self, "_weekly_calls", None) is None:
            days = np.array([bin(k).count("1") for k in range(256)], dtype=np.int64)
            calls = np.bincount(
                self.stop_si,
                weights=days[self.var_days[self.stop_variant]],
                minlength=len(self.station_id),
            )
            self._weekly_calls = {
                int(sid): int(n) for sid, n in zip(self.station_id, calls, strict=True)
            }
        return self._weekly_calls

    def _hop_graph(self):
        """Fastest scheduled hop between stations, as a graph indexed by the hop's far end.

        A hop joins two consecutive timed stops of a variant that runs on some day; its weight is
        the time from leaving the first to reaching the second, never negative. Any ride is a
        chain of hops (plus waiting), so shortest paths over the hops, whatever the day or hour,
        are lower bounds on the time a ride takes. Built once and shared by every search.
        """
        with _FLOOR_LOCK:
            graph = getattr(self, "_floor_graph", None)
            if graph is not None:
                return graph
            n_stations = len(self.station_id)
            position = np.arange(len(self.stop_si)) - self.var_first[self.stop_variant]
            t_in = np.where(self.stop_arr != NO_TIME, self.stop_arr, self.stop_dep)
            t_out = np.where(self.stop_dep != NO_TIME, self.stop_dep, self.stop_arr)
            keep = (
                (position < self.var_cut[self.stop_variant])
                & (t_in != NO_TIME)
                & (self.var_days[self.stop_variant] != 0)
            )
            rows = np.nonzero(keep)[0]
            before, after = rows[:-1], rows[1:]
            pair = (self.stop_variant[before] == self.stop_variant[after]) & (
                self.stop_si[before] != self.stop_si[after]
            )
            before, after = before[pair], after[pair]
            origin = self.stop_si[before].astype(np.int64)
            far = self.stop_si[after].astype(np.int64)
            weight = np.maximum(
                t_in[after].astype(np.int64) - t_out[before].astype(np.int64), 0
            )
            key = origin * n_stations + far
            order = np.lexsort((weight, key))
            key, origin, far, weight = key[order], origin[order], far[order], weight[order]
            first = np.ones(len(key), dtype=bool)
            first[1:] = key[1:] != key[:-1]
            origin, far, weight = origin[first], far[first], weight[first]
            by_far = np.argsort(far, kind="stable")
            start = np.searchsorted(far[by_far], np.arange(n_stations + 1))
            graph = (
                start.tolist(),
                origin[by_far].tolist(),
                weight[by_far].tolist(),
            )
            self._floor_graph = graph
            return graph

    def travel_floor(self, target_si):
        """Least seconds any ride could take from each station to the target.

        Index by compact station index; `UNREACHABLE_S` where no chain of scheduled hops leads
        there. Never more than the real ride time, so it is a safe A* heuristic.
        """
        start, origin, weight = self._hop_graph()
        floor = [UNREACHABLE_S] * len(self.station_id)
        floor[target_si] = 0
        heap = [(0, target_si)]
        while heap:
            seconds, si = heappop(heap)
            if seconds > floor[si]:
                continue
            for edge in range(start[si], start[si + 1]):
                u, total = origin[edge], seconds + weight[edge]
                if total < floor[u]:
                    floor[u] = total
                    heappush(heap, (total, u))
        return np.array(floor, dtype=np.int64)

    def departures(self, si, lo_epoch, hi_epoch):
        a, b = self.dep_start[si : si + 2]
        times, result = self.dep_time[a:b], []
        for service_date in dict.fromkeys(
            (
                local_date(lo_epoch) - timedelta(days=1),
                local_date(lo_epoch),
                local_date(hi_epoch),
            )
        ):
            anchor = service_date_anchor(service_date)
            i, j = (
                np.searchsorted(times, lo_epoch - anchor, side="left"),
                np.searchsorted(times, hi_epoch - anchor, side="right"),
            )
            k = day_key(service_date, self.station_state[si], self.holiday_set)
            for r in self.dep_rows[a + i : a + j]:
                v = int(self.stop_variant[r])
                if (int(self.var_days[v]) >> k) & 1:
                    result.append(
                        Departure(
                            v,
                            service_date,
                            int(r - self.var_first[v]),
                            anchor + int(self.stop_dep[r]),
                        )
                    )
        return sorted(result, key=lambda d: (d.dep_epoch, d.variant))


def _line_keys(session):
    keys = {
        int(tr): f"sl{line}"
        if line is not None
        else "c" + "".join(ch for ch in (category or "X") if ch.isalnum())
        for tr, line, category in session.execute(
            text("SELECT id, line_id, category FROM train")
        )
    }
    exists = session.execute(
        text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='passenger_line_member'"
        )
    ).first()
    if exists:
        keys.update(
            {
                int(tr): f"pl{line}"
                for tr, line in session.execute(
                    text(
                        "SELECT train_id, passenger_line_id FROM passenger_line_member"
                    )
                )
            }
        )
    return keys


def load_timetable(session):
    from dbrel.web.queries import tt_feed_version_id

    started = monotonic()
    fv = tt_feed_version_id(session)
    stations = list(
        session.execute(
            text("SELECT id, name, lat, lon, state FROM station ORDER BY id")
        )
    )
    profile = load_run_profile(session) if fv is not None else None
    chosen = chosen_variants(profile) if profile is not None else {}
    holiday_set = load_holiday_dates(session) if fv is not None else set()
    trains, codes, seqs, station_ids, arrs, deps = (
        array("q"),
        array("i"),
        array("i"),
        array("q"),
        array("i"),
        array("i"),
    )
    paths, path_codes = [], {}
    if fv is not None:
        cursor = session.connection().exec_driver_sql(
            "SELECT train_id, path_sha1, stop_sequence, station_id, planned_arrival_sec, planned_departure_sec "
            "FROM station_stop WHERE feed_version_id = :fv",
            {"fv": fv},
        )
        while rows := cursor.fetchmany(100_000):
            for tr, path, seq, station, arr, dep in rows:
                if (tr, path) not in profile.counts:
                    continue
                if path not in path_codes:
                    path_codes[path] = len(paths)
                    paths.append(path)
                trains.append(tr)
                codes.append(path_codes[path])
                seqs.append(seq)
                station_ids.append(station)
                arrs.append(NO_TIME if arr is None else arr)
                deps.append(NO_TIME if dep is None else dep)
    tt = PlannerTimetable.from_columns(
        stations=stations,
        train_id=np.frombuffer(trains, dtype=np.int64),
        path_code=np.frombuffer(codes, dtype=np.int32),
        path_values=paths,
        stop_sequence=np.frombuffer(seqs, dtype=np.int32),
        station_id=np.frombuffer(station_ids, dtype=np.int64),
        arr=np.frombuffer(arrs, dtype=np.int32),
        dep=np.frombuffer(deps, dtype=np.int32),
        chosen=chosen,
        line_key_of_train=_line_keys(session) if fv is not None else {},
        holiday_set=holiday_set,
        first_date=profile.first_date if profile else None,
        last_date=profile.last_date if profile else None,
    )
    log.info(
        "Planner timetable N=%d V=%d S=%d D=%d seconds=%.3f nbytes=%d",
        len(tt.stop_si),
        len(tt.var_train),
        len(tt.station_id),
        len(tt.dep_rows),
        monotonic() - started,
        tt.nbytes(),
    )
    return tt


_cache: dict[str, PlannerTimetable] = {}
_lock = Lock()


def cached_timetable(session):
    """The timetable if it is already loaded, else None -- never waits for a load."""
    return _cache.get(str(session.get_bind().url))


def get_timetable(session):
    key = str(session.get_bind().url)
    with _lock:
        if key not in _cache:
            _cache[key] = load_timetable(session)
            # The loader's sort/profile buffers are dead once it returns. Do
            # not retain their native allocations for the lifetime of the web worker.
            from dbrel.ml.memory import release_memory

            release_memory()
        return _cache[key]
