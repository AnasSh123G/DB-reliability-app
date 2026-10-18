"""Batched plan writer: the same database as calling `upsert_run` once per run.

`upsert_run` costs a handful of SELECTs and single-row writes per run. Here the
reads are done once per batch (bulk preload), the decisions are replayed in order
against in-memory state, and the writes go out as ordered executemany statements
(design D7). The decisions mirror `upsert_run` line for line, including the
fingerprint drift on a differently spelled head stop: `_normalize_run` is shared,
so the batch reproduces that behaviour rather than fixing it.

Explicit ids are allocated from max(id)+1, which is only safe with a single
writer; the backfill's writer lock provides that.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

from sqlalchemy import bindparam, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.identity import (
    IDENTITY_NUMBERED,
    IDENTITY_PATTERN,
    SOURCE_TT,
    line_aliases,
    line_display_name,
    line_identity_key,
    search_norm,
    segment_of,
    train_aliases,
    train_display_name,
)
from dbrel.ingest.dated_schedule import PlanReceipt, snapshot_rows
from dbrel.ingest.parser import TtStop
from dbrel.ingest.runs import (
    NormalizedRun,
    RunContext,
    RunCounters,
    _normalize_run,
    sec_of_day,
)
from dbrel.ingest.schedule_reconcile import PendingSettle
from dbrel.models import (
    SearchAlias,
    ServiceLine,
    StationStop,
    Train,
    Trip,
    TripScheduleSnapshot,
)

_CHUNK = 900

Run = tuple[str, list[TtStop], list[tuple[TtStop, PlanReceipt]] | None]

_SNAPSHOT_INSERT = sqlite_insert(TripScheduleSnapshot).on_conflict_do_nothing(
    index_elements=["trip_id", "stop_sequence", "observed_at",
                    "source_content_sha256", "value_fingerprint"],
).returning(TripScheduleSnapshot.trip_id, TripScheduleSnapshot.stop_sequence)

_TRAIN_UPDATE = (
    update(Train)
    .where(Train.id == bindparam("b_id"))
    .values(
        last_seen_date=bindparam("b_last_seen_date"),
        n_runs_seen=bindparam("b_n_runs_seen"),
        n_stops_tracked=bindparam("b_n_stops_tracked"),
    )
)
_TRIP_UPDATE = (
    update(Trip)
    .where(Trip.id == bindparam("b_id"))
    .values(
        supposed_departure_utc=bindparam("b_dep"),
        supposed_arrival_utc=bindparam("b_arr"),
        n_stops_tracked=bindparam("b_n_stops_tracked"),
        path_sha1=bindparam("b_path_sha1"),
    )
)


def _chunks(values: Sequence, size: int = _CHUNK) -> Iterator[Sequence]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


class _TripState:
    """A trip as the sequential pass sees it; only the writable fields matter."""

    __slots__ = ("id", "train_id", "service_date", "source", "source_run_key",
                 "path_sha1", "feed_version_id", "dep", "arr", "n_stops_tracked",
                 "insert", "dirty")

    def __init__(self, **fields) -> None:
        for name in self.__slots__:
            setattr(self, name, fields.get(name))


class _StationStops:
    """Existing station_stop rows for one (feed_version, train, path)."""

    __slots__ = ("seqs", "station_seq", "ids")

    def __init__(self) -> None:
        self.seqs: set[int] = set()
        self.station_seq: dict[int, int] = {}
        self.ids: dict[tuple[int, int], int] = {}

    def add(self, row_id: int, seq: int, station_id: int) -> None:
        self.seqs.add(seq)
        self.station_seq.setdefault(station_id, seq)
        self.ids[(seq, station_id)] = row_id


class _Batch:
    def __init__(self, session: Session, ctx: RunContext, counters: RunCounters,
                 pending: PendingSettle) -> None:
        self.session, self.ctx, self.counters, self.pending = session, ctx, counters, pending
        self.trains: dict[int, dict] = {}
        self.trips_by_train_date: dict[tuple[int, str], _TripState] = {}
        self.trips_by_key: dict[tuple[str, str, str], _TripState] = {}
        self.stops: dict[tuple[int, int, str], _StationStops] = {}
        self.new_lines: list[dict] = []
        self.new_trains: list[dict] = []
        self.new_train_ids: set[int] = set()
        self.aliases: list[dict] = []
        self.new_stops: list[dict] = []
        self.new_trips: list[_TripState] = []
        self.snapshots: list[dict] = []
        self.buffered_trips: dict[int, None] = {}
        self.dirty_trains: dict[int, dict] = {}
        self.dirty_trips: dict[int, _TripState] = {}
        self.next_id = {
            model: (session.scalar(select(func.max(model.id))) or 0) + 1
            for model in (ServiceLine, Train, StationStop, Trip)
        }

    # -- ids -----------------------------------------------------------------
    def _allocate(self, model) -> int:
        value = self.next_id[model]
        self.next_id[model] = value + 1
        return value

    # -- bulk preload --------------------------------------------------------
    def preload(self, normalized: list[NormalizedRun], run_keys: list[str]) -> None:
        session, ctx = self.session, self.ctx
        line_keys = sorted({
            line_identity_key(n.head.category, n.head.line)
            for n in normalized if n.head.category and n.head.line
        } - set(ctx.line_cache))
        for keys in _chunks(line_keys):
            for row_id, key in session.execute(
                select(ServiceLine.id, ServiceLine.identity_key).where(
                    ServiceLine.identity_key.in_(keys))
            ):
                ctx.line_cache[key] = row_id

        train_ids: set[int] = set()
        for n in normalized:
            for identity in (n.identity, f"{n.identity}#{n.digest[:8]}"):
                known = ctx.train_by_key.get(identity)
                if known is not None:
                    train_ids.add(known)
        dates = sorted({n.head.service_date for n in normalized})
        trip_train_ids: set[int] = set(train_ids)
        loaded: dict[int, _TripState] = {}

        def load_trips(condition) -> None:
            for row in session.execute(
                select(Trip.id, Trip.train_id, Trip.service_date, Trip.source,
                       Trip.source_run_key, Trip.path_sha1, Trip.feed_version_id,
                       Trip.supposed_departure_utc, Trip.supposed_arrival_utc,
                       Trip.n_stops_tracked)
                .where(Trip.service_date.in_(dates), condition)
            ):
                if row.id in loaded:
                    continue
                state = _TripState(
                    id=row.id, train_id=row.train_id, service_date=row.service_date,
                    source=row.source, source_run_key=row.source_run_key,
                    path_sha1=row.path_sha1, feed_version_id=row.feed_version_id,
                    dep=row.supposed_departure_utc, arr=row.supposed_arrival_utc,
                    n_stops_tracked=row.n_stops_tracked,
                )
                loaded[row.id] = state
                self.trips_by_train_date[(row.train_id, row.service_date)] = state
                self.trips_by_key[(row.source, row.source_run_key, row.service_date)] = state
                trip_train_ids.add(row.train_id)

        for keys in _chunks(sorted(set(run_keys)), 450):
            load_trips((Trip.source == SOURCE_TT) & Trip.source_run_key.in_(keys))
        for ids in _chunks(sorted(train_ids), 450):
            load_trips(Trip.train_id.in_(ids))

        for ids in _chunks(sorted(train_ids)):
            for row in session.execute(
                select(Train.id, Train.path_sha1, Train.operator, Train.last_seen_date,
                       Train.n_runs_seen, Train.n_stops_tracked).where(Train.id.in_(ids))
            ):
                self.trains[row.id] = {
                    "id": row.id, "path_sha1": row.path_sha1, "operator": row.operator,
                    "last_seen_date": row.last_seen_date, "n_runs_seen": row.n_runs_seen,
                    "n_stops_tracked": row.n_stops_tracked,
                }
        for ids in _chunks(sorted(trip_train_ids)):
            for row in session.execute(
                select(StationStop.id, StationStop.feed_version_id, StationStop.train_id,
                       StationStop.path_sha1, StationStop.stop_sequence,
                       StationStop.station_id)
                .where(StationStop.train_id.in_(ids)).order_by(StationStop.id)
            ):
                key = (row.feed_version_id, row.train_id, row.path_sha1)
                self.stops.setdefault(key, _StationStops()).add(
                    row.id, row.stop_sequence, row.station_id)

    # -- sequential pass -----------------------------------------------------
    def _line(self, head: TtStop) -> int | None:
        if not head.category or not head.line:
            return None
        key = line_identity_key(head.category, head.line)
        cached = self.ctx.line_cache.get(key)
        if cached is not None:
            return cached
        line_id = self._allocate(ServiceLine)
        display = line_display_name(head.category, head.line)
        self.new_lines.append({
            "id": line_id, "identity_key": key, "category": head.category,
            "line": head.line, "operator": head.owner,
            "segment": segment_of(head.category, head.line),
            "display_name": display, "search_name": search_norm(display),
        })
        for alias_norm, alias_display, kind, weight in line_aliases(head.category, head.line):
            self.aliases.append({
                "alias_norm": alias_norm, "alias_display": alias_display, "kind": kind,
                "weight": weight, "train_id": None, "line_id": line_id,
            })
        self.ctx.line_cache[key] = line_id
        return line_id

    def _train(self, train_id: int) -> dict:
        state = self.trains.get(train_id)
        if state is None:  # not preloaded: fall back to one read
            row = self.session.execute(
                select(Train.id, Train.path_sha1, Train.operator, Train.last_seen_date,
                       Train.n_runs_seen, Train.n_stops_tracked).where(Train.id == train_id)
            ).one()
            state = dict(row._mapping)
            self.trains[train_id] = state
        return state

    def resolve_train(self, norm: NormalizedRun, line_id: int | None) -> tuple[int, dict]:
        ctx, head, digest = self.ctx, norm.head, norm.digest
        identity = norm.identity
        train_id = ctx.train_by_key.get(identity)
        if train_id is not None:
            train = self._train(train_id)
            if (train["path_sha1"] and train["path_sha1"] != digest
                    and train["operator"] and train["operator"] != head.owner):
                self.counters.conflicts += 1
                identity = f"{identity}#{digest[:8]}"
                train_id = ctx.train_by_key.get(identity)

        stops, full_path = norm.stops, norm.full_path
        if train_id is None:
            train_id = self._allocate(Train)
            display = train_display_name(head.category, head.number)
            state = {
                "id": train_id, "source": SOURCE_TT,
                "identity_kind": IDENTITY_NUMBERED if head.number else IDENTITY_PATTERN,
                "identity_key": identity,
                "train_number": int(head.number) if head.number and head.number.isdigit() else None,
                "train_number_raw": head.number, "category": head.category,
                "operator": head.owner, "trip_type": head.trip_type,
                "segment": segment_of(head.category, head.line), "line_id": line_id,
                "display_name": display, "search_name": search_norm(display),
                "passenger_label": head.label,
                "long_name": f"{display} {full_path[0]} → {full_path[-1]}" if full_path else display,
                "origin_name": full_path[0] if full_path else None,
                "dest_name": full_path[-1] if full_path else None,
                "route_path": "|".join(full_path), "path_sha1": digest,
                "n_stops": len(full_path), "n_stops_tracked": len(stops),
                "first_departure_sec": int(head.start_stamp[6:8]) * 3600
                + int(head.start_stamp[8:]) * 60,
                "first_seen_date": head.service_date, "last_seen_date": head.service_date,
                "n_runs_seen": 1, "first_seen_feed_version_id": ctx.fv_id,
                "last_seen_feed_version_id": ctx.fv_id,
            }
            self.trains[train_id] = state
            self.new_trains.append(state)
            self.new_train_ids.add(train_id)
            ctx.train_by_key[identity] = train_id
            self.counters.trains_created += 1
            for alias_norm, alias_display, kind, weight in train_aliases(head.category, head.number):
                self.aliases.append({
                    "alias_norm": alias_norm, "alias_display": alias_display, "kind": kind,
                    "weight": weight, "train_id": train_id, "line_id": None,
                })
        else:
            state = self._train(train_id)
            state["last_seen_date"] = max(state["last_seen_date"] or "", head.service_date)
            state["n_runs_seen"] = (state["n_runs_seen"] or 0) + 1
            state["n_stops_tracked"] = max(state["n_stops_tracked"] or 0, len(stops))
            if train_id not in self.new_train_ids:
                self.dirty_trains[train_id] = state
        return train_id, state

    def station_stops(self, train_id: int, norm: NormalizedRun) -> None:
        ctx, digest, head = self.ctx, norm.digest, norm.head
        existing = self.stops.setdefault((ctx.fv_id, train_id, digest), _StationStops())
        rows = []
        for stop in norm.stops:
            if stop.stop_index in existing.seqs:
                continue
            station_id = ctx.station_id_by_eva.get(stop.eva)
            if station_id is None:
                continue
            pinned = existing.station_seq.get(station_id)
            if pinned is not None and pinned != stop.stop_index:
                self.counters.pattern_conflicts += 1
                continue
            side = stop.departure or stop.arrival
            rows.append({
                "id": self._allocate(StationStop),
                "feed_version_id": ctx.fv_id, "train_id": train_id, "path_sha1": digest,
                "stop_sequence": stop.stop_index, "station_id": station_id,
                "source_stop_id": str(stop.eva),
                "platform_code": side.planned_platform if side else None,
                "planned_arrival_sec": sec_of_day(stop.arrival.planned_ts, head.service_date)
                if stop.arrival else None,
                "planned_departure_sec": sec_of_day(stop.departure.planned_ts, head.service_date)
                if stop.departure else None,
            })
        for row in rows:  # visible to later runs only, as after the per-run insert
            existing.add(row["id"], row["stop_sequence"], row["station_id"])
        if rows:
            self.new_stops.extend(rows)
            self.counters.station_stops_written += len(rows)

    def trip(self, train_id: int, run_key: str, norm: NormalizedRun) -> _TripState:
        head, digest, stops = norm.head, norm.digest, norm.stops
        first_dep = next((s.departure.planned_ts for s in stops if s.departure), None)
        last_arr = next((s.arrival.planned_ts for s in reversed(stops) if s.arrival), None)
        found = (self.trips_by_train_date.get((train_id, head.service_date))
                 or self.trips_by_key.get((SOURCE_TT, run_key, head.service_date)))
        self.counters.trips_created += 1
        if found is not None:
            if found.path_sha1 != digest and (
                found.id in self.pending.dirty or found.id in self.buffered_trips
            ):
                self.flush()
            found.dep, found.arr = first_dep, last_arr
            found.n_stops_tracked, found.path_sha1 = len(stops), digest
            self._mark(found)
            return found
        state = _TripState(
            id=self._allocate(Trip), train_id=train_id, service_date=head.service_date,
            source=SOURCE_TT, source_run_key=run_key, path_sha1=digest,
            feed_version_id=self.ctx.fv_id, dep=first_dep, arr=last_arr,
            n_stops_tracked=len(stops),
            insert={
                "daily_trip_id": head.daily_trip_id, "status": "SCHEDULED",
                "schedule_relationship": "PLANNED",
                "endpoints_tracked": int(len(stops) == len(norm.full_path)),
                "n_stops": len(norm.full_path),
            },
        )
        self.trips_by_train_date[(train_id, head.service_date)] = state
        self.trips_by_key[(SOURCE_TT, run_key, head.service_date)] = state
        self.new_trips.append(state)
        return state

    def _mark(self, trip: _TripState) -> None:
        if trip.insert is None:
            self.dirty_trips[trip.id] = trip

    def plans(self, trip: _TripState, digest: str,
              observations: Iterable[tuple[TtStop, PlanReceipt]]) -> None:
        rows = snapshot_rows(trip, digest, observations, self.ctx.station_id_by_eva)
        if not rows:
            return
        links = {}
        if trip.feed_version_id is not None:
            known = self.stops.get((trip.feed_version_id, trip.train_id, digest))
            links = known.ids if known is not None else {}
        for row in rows:
            row["station_stop_id"] = links.get((row["stop_sequence"], row["station_id"]))
        self.snapshots.extend(rows)
        self.buffered_trips[trip.id] = None

    # -- epoch flush ---------------------------------------------------------
    def flush(self) -> None:
        session = self.session
        session.flush()
        conn = session.connection()
        if self.new_lines:
            conn.execute(ServiceLine.__table__.insert(), self.new_lines)
        if self.new_trains:
            conn.execute(Train.__table__.insert(), self.new_trains)
        if self.aliases:
            conn.execute(SearchAlias.__table__.insert(), self.aliases)
        if self.dirty_trains:
            conn.execute(_TRAIN_UPDATE, [
                {"b_id": t["id"], "b_last_seen_date": t["last_seen_date"],
                 "b_n_runs_seen": t["n_runs_seen"], "b_n_stops_tracked": t["n_stops_tracked"]}
                for t in self.dirty_trains.values()
            ])
        if self.new_stops:
            conn.execute(StationStop.__table__.insert(), self.new_stops)
        if self.new_trips:
            conn.execute(Trip.__table__.insert(), [
                {
                    "id": t.id, "train_id": t.train_id, "service_date": t.service_date,
                    "source": t.source, "source_run_key": t.source_run_key,
                    "feed_version_id": t.feed_version_id,
                    "supposed_departure_utc": t.dep, "supposed_arrival_utc": t.arr,
                    "n_stops_tracked": t.n_stops_tracked, "path_sha1": t.path_sha1,
                    **t.insert,
                }
                for t in self.new_trips
            ])
        if self.dirty_trips:
            conn.execute(_TRIP_UPDATE, [
                {"b_id": t.id, "b_dep": t.dep, "b_arr": t.arr,
                 "b_n_stops_tracked": t.n_stops_tracked, "b_path_sha1": t.path_sha1}
                for t in self.dirty_trips.values()
            ])
        inserted: dict[int, set[int]] = {}
        if self.snapshots:
            for trip_id, sequence in conn.execute(_SNAPSHOT_INSERT, self.snapshots):
                inserted.setdefault(trip_id, set()).add(sequence)
        # The reference refreshes bounds for every trip it recorded plans for,
        # even when every snapshot was a replay.
        for trip_id in self.buffered_trips:
            self.pending.add(trip_id, inserted.get(trip_id, set()))
        for trip in self.new_trips:
            trip.insert = None
        self.new_lines, self.new_trains, self.aliases = [], [], []
        self.new_train_ids = set()
        self.new_stops, self.new_trips, self.snapshots = [], [], []
        self.buffered_trips, self.dirty_trains, self.dirty_trips = {}, {}, {}
        self.counters.batch_epochs += 1
        # Raw Core writes bypass the identity map, and settling reads ORM attributes.
        session.expire_all()
        self.pending.settle(session)


def upsert_runs_batch(
    session: Session,
    ctx: RunContext,
    counters: RunCounters,
    runs: list[Run],
    *,
    pending: PendingSettle | None = None,
) -> None:
    """Write `runs` in order, producing what per-run `upsert_run` calls would."""
    if not runs:
        return
    pending = pending if pending is not None else PendingSettle()
    normalized = []
    for _run_key, stops, observations in runs:
        counters.upsert_calls += 1
        normalized.append(_normalize_run(ctx, counters, stops, observations))

    session.flush()
    batch = _Batch(session, ctx, counters, pending)
    batch.preload(normalized, [run_key for run_key, _s, _o in runs])
    for (run_key, _stops, _obs), norm in zip(runs, normalized):
        line_id = batch._line(norm.head)
        train_id, _state = batch.resolve_train(norm, line_id)
        batch.station_stops(train_id, norm)
        trip = batch.trip(train_id, run_key, norm)
        if norm.observations:
            batch.plans(trip, norm.digest, norm.observations)
    batch.flush()
