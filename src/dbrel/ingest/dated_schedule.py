"""Append-only dated schedule evidence and bounded schedule resolution."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.identity import SOURCE_TT
from dbrel.models import (
    StationStop, Trip, TripScheduleSnapshot,
)
from dbrel.ingest.parser import TtStop


@dataclass(frozen=True, slots=True)
class PlanReceipt:
    observed_at: int
    content_sha256: str
    source_kind: str = "plan"
    eva: int | None = None
    plan_date: str | None = None
    plan_hour: int | None = None


@dataclass(frozen=True, slots=True)
class DatedPlanned:
    station_stop_id: int | None
    station_id: int
    platform_code: str | None
    supposed_arr: int | None
    supposed_dep: int | None


def _valid_receipt(receipt: PlanReceipt) -> None:
    if not isinstance(receipt, PlanReceipt):
        raise TypeError("receipt must be PlanReceipt")
    if type(receipt.observed_at) is not int or receipt.observed_at < 0:
        raise ValueError("receipt observed_at must be a nonnegative integer")
    if receipt.source_kind not in {"plan", "fchg"}:
        raise ValueError("source_kind must be plan or fchg")
    if not isinstance(receipt.content_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", receipt.content_sha256):
        raise ValueError("content_sha256 must be 64 hexadecimal characters")
    if receipt.eva is not None and (type(receipt.eva) is not int or receipt.eva <= 0):
        raise ValueError("receipt EVA must be positive")
    if receipt.plan_hour is not None and (type(receipt.plan_hour) is not int or not 0 <= receipt.plan_hour <= 23):
        raise ValueError("plan_hour must be between 0 and 23")
    if receipt.plan_date is not None:
        if not isinstance(receipt.plan_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", receipt.plan_date):
            raise ValueError("plan_date must be an ISO calendar date")
        date.fromisoformat(receipt.plan_date)


def record_plans(
    session: Session,
    trip_id: int,
    path_sha1: str,
    observations: Iterable[tuple[TtStop, PlanReceipt]],
    station_id_by_eva: Mapping[int, int],
) -> int:
    """Store validated observations, skipping exact receipt/value replays."""
    trip = session.get(Trip, trip_id)
    if trip is None or trip.source != SOURCE_TT:
        raise ValueError("dated plans require an existing TT trip")
    if not path_sha1 or trip.path_sha1 != path_sha1:
        raise ValueError("path does not match trip")
    batch: list[TripScheduleSnapshot] = []
    for stop, receipt in observations:
        if not isinstance(stop, TtStop):
            raise TypeError("observations must contain TtStop values")
        _valid_receipt(receipt)
        if stop.service_date != trip.service_date:
            raise ValueError("observation service date does not match trip")
        if type(stop.stop_index) is not int or stop.stop_index <= 0:
            raise ValueError("stop sequence must be positive")
        if receipt.eva is not None and stop.eva != receipt.eva:
            raise ValueError("receipt EVA does not match observation")
        if stop.eva is None or stop.eva not in station_id_by_eva:
            raise ValueError("observation EVA has no station mapping")
        station_id = station_id_by_eva[stop.eva]
        arr = stop.arrival.planned_ts if stop.arrival else None
        dep = stop.departure.planned_ts if stop.departure else None
        if any(value is not None and (type(value) is not int or value < 0) for value in (arr, dep)):
            raise ValueError("planned timestamps must be nonnegative integers")
        if arr is None and dep is None:
            continue
        platform_values = {e.planned_platform for e in (stop.arrival, stop.departure) if e and e.planned_platform}
        platform = next(iter(platform_values)) if len(platform_values) == 1 else None
        values = (stop.stop_index, station_id, arr, dep, platform)
        fingerprint = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
        station_stop_id = session.scalar(select(StationStop.id).where(
            StationStop.train_id == trip.train_id,
            StationStop.feed_version_id == trip.feed_version_id,
            StationStop.path_sha1 == path_sha1,
            StationStop.stop_sequence == stop.stop_index,
            StationStop.station_id == station_id,
        )) if trip.feed_version_id is not None else None
        batch.append(TripScheduleSnapshot(
            trip_id=trip.id, stop_sequence=stop.stop_index, station_id=station_id,
            station_stop_id=station_stop_id, path_sha1=path_sha1,
            supposed_arrival_utc=arr, supposed_departure_utc=dep,
            platform_code=platform, observed_at=receipt.observed_at,
            source_kind=receipt.source_kind, source_content_sha256=receipt.content_sha256.lower(),
            source_stop_id=stop.stop_id, source_eva=stop.eva,
            source_plan_date=receipt.plan_date, source_plan_hour=receipt.plan_hour,
            value_fingerprint=fingerprint,
        ))
    if not batch:
        return 0
    # The existing unique index handles duplicates, including concurrent replay.
    # Bound each statement below SQLite's traditional 999-parameter ceiling;
    # never load a trip's entire receipt history to append one observation.
    columns = [column.name for column in TripScheduleSnapshot.__table__.columns if column.name != 'id']
    inserted_sequences = []
    identity = ['trip_id', 'stop_sequence', 'observed_at',
                'source_content_sha256', 'value_fingerprint']
    for rows in _chunks(batch, 40):
        statement = sqlite_insert(TripScheduleSnapshot).values([
            {name: getattr(row, name) for name in columns} for row in rows
        ]).on_conflict_do_nothing(index_elements=identity).returning(TripScheduleSnapshot.stop_sequence)
        inserted_sequences.extend(session.scalars(statement))
    session.flush()
    from dbrel.ingest.schedule_reconcile import reconcile_facts, refresh_run_bounds
    refresh_run_bounds(session, trip)
    if inserted_sequences:
        reconcile_facts(session, trip, set(inserted_sequences))
    return len(inserted_sequences)


def _chunks(values: list, size: int):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _unique(values: set[int]) -> int | None:
    return next(iter(values)) if len(values) == 1 else None


def load_patterns(
    session: Session,
    train_ids: Iterable[int],
    dates_by_train: Mapping[int, Mapping[str, str | None]],
    *,
    as_of: int | None = None,
) -> dict[tuple[int, str], dict[int, DatedPlanned]]:
    """Resolve requested dated train identities without cross-date leakage."""
    requested = sorted({
        (int(tid), service_date, path_sha1)
        for tid in train_ids
        for service_date, path_sha1 in dates_by_train.get(tid, {}).items()
    })
    result = {(tid, day): {} for tid, day, _path in requested}
    if not requested:
        return result
    matched_trips: dict[tuple[int, str], Trip] = {}
    for pairs in _chunks(requested, 400):
        clauses = [and_(Trip.train_id == tid, Trip.service_date == day) for tid, day, _path in pairs]
        for trip in session.scalars(select(Trip).where(or_(*clauses))):
            matched_trips[(trip.train_id, trip.service_date)] = trip
    trips = list(matched_trips.values())
    # Receipt identity is preserved in storage. Resolution needs only distinct
    # planned values, so repeated polls must not hydrate every historical ORM row.
    snapshots: dict[int, list] = defaultdict(list)
    for ids in _chunks([t.id for t in trips], 900):
        query = select(
            TripScheduleSnapshot.trip_id, TripScheduleSnapshot.path_sha1,
            TripScheduleSnapshot.stop_sequence, TripScheduleSnapshot.station_id,
            TripScheduleSnapshot.station_stop_id, TripScheduleSnapshot.supposed_arrival_utc,
            TripScheduleSnapshot.supposed_departure_utc, TripScheduleSnapshot.platform_code,
        ).where(TripScheduleSnapshot.trip_id.in_(ids)).distinct()
        if as_of is not None:
            query = query.where(TripScheduleSnapshot.observed_at <= as_of)
        for snap in session.execute(query):
            snapshots[snap.trip_id].append(snap)
    wanted_paths = {(tid, day): path for tid, day, path in requested}
    for pair, trip in matched_trips.items():
        wanted_path = wanted_paths.get(pair)
        if trip.path_sha1 != wanted_path:
            continue
        if trip.source == SOURCE_TT:
            grouped: dict[int, list[TripScheduleSnapshot]] = defaultdict(list)
            for snap in snapshots[trip.id]:
                if snap.path_sha1 == trip.path_sha1 and snap.path_sha1 == wanted_path:
                    grouped[snap.stop_sequence].append(snap)
            for seq, rows in grouped.items():
                stations = {r.station_id for r in rows}
                if len(stations) != 1:
                    continue
                arr = _unique({r.supposed_arrival_utc for r in rows if r.supposed_arrival_utc is not None})
                dep = _unique({r.supposed_departure_utc for r in rows if r.supposed_departure_utc is not None})
                platforms = {r.platform_code for r in rows if r.platform_code}
                links = {r.station_stop_id for r in rows if r.station_stop_id is not None}
                result[pair][seq] = DatedPlanned(_unique(links), next(iter(stations)), _unique_platform(platforms), arr, dep)
        elif trip.feed_version_id is not None:
            from dbrel.ingest.runs import epoch_of_sec
            stmt = select(StationStop).where(
                StationStop.train_id == trip.train_id,
                StationStop.feed_version_id == trip.feed_version_id,
                StationStop.path_sha1 == trip.path_sha1,
            )
            for ss in session.scalars(stmt):
                result[pair][ss.stop_sequence] = DatedPlanned(
                    ss.id, ss.station_id, ss.platform_code,
                    epoch_of_sec(ss.planned_arrival_sec, trip.service_date),
                    epoch_of_sec(ss.planned_departure_sec, trip.service_date),
                )
    return result


def _unique_platform(values: set[str]) -> str | None:
    return next(iter(values)) if len(values) == 1 else None
