"""Import a static GTFS archive into the core tables.

Shape of the gtfs.de feeds (verified — see docs/probe-findings.md):

  trips.txt      route_id, service_id, trip_id            <- nothing else
  routes.txt     route_short_name is the product (ICE/EC/RE), agency_id
  stop_times.txt stop_headsign carries the destination
  stops.txt      parent stations (location_type=1) + child platforms, integer ids

There is no train number anywhere in these feeds, so a train is identified by what
it does: product, operator, departure time, and the ordered list of stations it
calls at. See train_natural_key.

What this import is *for* has narrowed. GTFS is the reference skeleton — station
names and coordinates, and the route patterns tt-rank-stations and
tt-verify-stations need — not a join target. A GTFS train and its IRIS counterpart
share no identifier (GTFS carries no train numbers at all), so nothing here ever
joins to a recorded actual. Expanding the calendar into 1.27M dated `trip` rows
therefore materialised a third of the database to answer a question nothing asked;
the calendar is still read, to date-stamp the feed version, but not written out.
"""

from __future__ import annotations

import csv
import hashlib
import io
import time
import unicodedata
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from dbrel.config import RAIL_ROUTE_TYPES
from dbrel.gtfs.timeutil import date_range, parse_gtfs_time, parse_service_date
from dbrel.identity import (
    IDENTITY_PATTERN,
    SOURCE_GTFS,
    search_norm,
    segment_of,
    train_identity_key,
)
from dbrel.models import FeedVersion, Route, Station, StationStop, Train

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass
class ImportStats:
    feed_version_id: int | None = None
    skipped_reason: str | None = None
    stations: int = 0
    routes: int = 0
    trains: int = 0
    station_stops: int = 0
    gtfs_trips_read: int = 0
    non_rail_trips: int = 0
    elapsed_s: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)


def _rows(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    if name not in zf.namelist():
        return
    with zf.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))


def normalize_name(name: str) -> str:
    """Casefold + strip accents, so 'Aachen, Hbf' and 'AACHEN, HBF' agree."""
    folded = unicodedata.normalize("NFKD", name.strip())
    return "".join(c for c in folded if not unicodedata.combining(c)).casefold()


def station_natural_key(name: str, lat: float, lon: float) -> str:
    """Identity for a station across feed rebuilds.

    gtfs.de stop_ids are opaque integers with no documented stability, so name plus
    position is used instead. Three decimals is ~110 m: enough to separate the six
    rv stations that share a name, coarse enough to absorb coordinate jitter.
    """
    return f"{normalize_name(name)}@{lat:.3f},{lon:.3f}"


def train_natural_key(
    product: str | None, agency_id: str | None, first_departure_sec: int | None, station_keys: list[str]
) -> str:
    """Identity for a train across feed rebuilds.

    Keyed on what the service *is*: product, operator, departure time and the exact
    sequence of stations. Departure time has to be in the key or every train on an
    hourly line would collapse into one; the cost is that a retimed train reads as a
    new train, which is arguably correct anyway.
    """
    digest = hashlib.sha1("|".join(station_keys).encode(), usedforsecurity=False).hexdigest()[:16]
    return f"{product or '?'}/{agency_id or '?'}/{first_departure_sec if first_departure_sec is not None else '?'}/{digest}"


@dataclass(slots=True)
class _StopTime:
    seq: int
    stop_id: str
    arr: int | None
    dep: int | None
    headsign: str | None
    pickup: int | None
    dropoff: int | None


def service_dates(zf: zipfile.ZipFile) -> dict[str, set[str]]:
    """service_id -> ISO dates it runs on, from calendar.txt + calendar_dates.txt."""
    result: dict[str, set[str]] = defaultdict(set)
    for row in _rows(zf, "calendar.txt"):
        start = parse_service_date(row["start_date"])
        end = parse_service_date(row["end_date"])
        active = [i for i, day in enumerate(WEEKDAYS) if row.get(day) == "1"]
        if not active:
            continue
        wanted = set(active)
        for day in date_range(start, end):
            if day.weekday() in wanted:
                result[row["service_id"]].add(day.isoformat())
    for row in _rows(zf, "calendar_dates.txt"):
        iso = parse_service_date(row["date"]).isoformat()
        if row["exception_type"] == "1":
            result[row["service_id"]].add(iso)
        elif row["exception_type"] == "2":
            result[row["service_id"]].discard(iso)
    return result


def import_feed(
    session: Session,
    *,
    kind: str,
    url: str,
    content: bytes,
    sha256: str,
    etag: str | None = None,
    last_modified: str | None = None,
    force: bool = False,
    progress=lambda msg: None,
) -> ImportStats:
    """Import one static GTFS archive. Idempotent on content hash."""
    stats = ImportStats()
    started = time.monotonic()

    existing = session.scalar(select(FeedVersion).where(FeedVersion.content_sha256 == sha256))
    if existing is not None and existing.status == "active" and not force:
        stats.feed_version_id = existing.id
        stats.skipped_reason = f"already imported as feed_version {existing.id}"
        return stats
    if existing is not None:
        # A previous attempt failed or --force was passed: clear its rows and redo.
        progress(f"  replacing feed_version {existing.id} ({existing.status})")
        _purge_feed_version(session, existing.id)
        session.delete(existing)
        session.flush()

    now = int(time.time())
    feed_version = FeedVersion(
        kind=kind,
        source_url=url,
        etag=etag,
        last_modified=last_modified,
        content_sha256=sha256,
        size_bytes=len(content),
        downloaded_at=now,
        status="importing",
    )
    session.add(feed_version)
    session.flush()
    stats.feed_version_id = feed_version.id
    fv_id = feed_version.id

    zf = zipfile.ZipFile(io.BytesIO(content))

    # --- stations ---------------------------------------------------------
    progress("  reading stops ...")
    parents: dict[str, dict[str, str]] = {}
    children: dict[str, dict[str, str]] = {}
    for row in _rows(zf, "stops.txt"):
        (parents if row.get("location_type") == "1" else children)[row["stop_id"]] = row

    station_id_by_key = dict(
        session.execute(select(Station.natural_key, Station.id)).all()
    )
    new_stations = []
    key_by_gtfs_stop: dict[str, str] = {}
    for stop_id, row in parents.items():
        key = station_natural_key(row["stop_name"], float(row["stop_lat"]), float(row["stop_lon"]))
        key_by_gtfs_stop[stop_id] = key
        if key not in station_id_by_key:
            station_id_by_key[key] = None  # placeholder, filled after insert
            new_stations.append(
                {
                    "natural_key": key,
                    "name": row["stop_name"],
                    "lat": float(row["stop_lat"]),
                    "lon": float(row["stop_lon"]),
                    "first_seen_feed_version_id": fv_id,
                }
            )
    if new_stations:
        session.execute(insert(Station), new_stations)
        session.flush()
    station_id_by_key = dict(session.execute(select(Station.natural_key, Station.id)).all())
    stats.stations = len(new_stations)

    # A stop_time references a platform; the station is its parent.
    station_id_by_stop: dict[str, int] = {}
    platform_by_stop: dict[str, str | None] = {}
    for stop_id, row in children.items():
        parent = row.get("parent_station") or ""
        key = key_by_gtfs_stop.get(parent)
        if key is None:
            # Childless stop used directly by stop_times: treat it as its own station.
            key = station_natural_key(row["stop_name"], float(row["stop_lat"]), float(row["stop_lon"]))
            if key not in station_id_by_key:
                session.execute(
                    insert(Station),
                    [
                        {
                            "natural_key": key,
                            "name": row["stop_name"],
                            "lat": float(row["stop_lat"]),
                            "lon": float(row["stop_lon"]),
                            "first_seen_feed_version_id": fv_id,
                        }
                    ],
                )
                session.flush()
                station_id_by_key[key] = session.scalar(
                    select(Station.id).where(Station.natural_key == key)
                )
                stats.stations += 1
        station_id_by_stop[stop_id] = station_id_by_key[key]
        platform_by_stop[stop_id] = row.get("platform_code") or None
    for stop_id, key in key_by_gtfs_stop.items():
        station_id_by_stop.setdefault(stop_id, station_id_by_key[key])
        platform_by_stop.setdefault(stop_id, parents[stop_id].get("platform_code") or None)

    # --- routes -----------------------------------------------------------
    progress("  reading routes ...")
    route_rows = []
    route_meta: dict[str, dict[str, str]] = {}
    for row in _rows(zf, "routes.txt"):
        route_meta[row["route_id"]] = row
        route_rows.append(
            {
                "feed_version_id": fv_id,
                "gtfs_route_id": row["route_id"],
                "agency_id": row.get("agency_id") or None,
                "short_name": row.get("route_short_name") or None,
                "long_name": row.get("route_long_name") or None,
                "route_type": int(row.get("route_type") or 0),
            }
        )
    if route_rows:
        session.execute(insert(Route), route_rows)
        session.flush()
    stats.routes = len(route_rows)
    route_pk_by_gtfs = dict(
        session.execute(
            select(Route.gtfs_route_id, Route.id).where(Route.feed_version_id == fv_id)
        ).all()
    )

    # --- trips + stop_times ----------------------------------------------
    progress("  reading trips ...")
    trip_route: dict[str, str] = {}
    for row in _rows(zf, "trips.txt"):
        stats.gtfs_trips_read += 1
        route = route_meta.get(row["route_id"])
        if route is None or int(route.get("route_type") or 0) not in RAIL_ROUTE_TYPES:
            stats.non_rail_trips += 1
            continue
        trip_route[row["trip_id"]] = row["route_id"]

    progress(f"  reading stop_times for {len(trip_route):,} rail trips ...")
    by_trip: dict[str, list[_StopTime]] = defaultdict(list)
    for row in _rows(zf, "stop_times.txt"):
        trip_id = row["trip_id"]
        if trip_id not in trip_route:
            continue
        by_trip[trip_id].append(
            _StopTime(
                seq=int(row["stop_sequence"]),
                stop_id=row["stop_id"],
                arr=parse_gtfs_time(row.get("arrival_time")),
                dep=parse_gtfs_time(row.get("departure_time")),
                headsign=row.get("stop_headsign") or None,
                pickup=int(row["pickup_type"]) if row.get("pickup_type") else None,
                dropoff=int(row["drop_off_type"]) if row.get("drop_off_type") else None,
            )
        )
    for stops in by_trip.values():
        stops.sort(key=lambda s: s.seq)

    # --- trains -----------------------------------------------------------
    progress("  building trains ...")
    train_id_by_key = dict(session.execute(select(Train.identity_key, Train.id)).all())
    new_trains: dict[str, dict] = {}
    pattern_by_key: dict[str, list[_StopTime]] = {}
    train_key_by_trip: dict[str, str] = {}

    for trip_id, stops in by_trip.items():
        if not stops:
            continue
        route_gtfs_id = trip_route[trip_id]
        route = route_meta[route_gtfs_id]
        station_keys = [key_for_stop(s.stop_id, station_id_by_stop) for s in stops]
        first_dep = next((s.dep for s in stops if s.dep is not None), None)
        # The legacy pattern key is kept verbatim, only source-prefixed, so GTFS
        # and Timetables identities can never collide while both sources run.
        key = train_identity_key(
            SOURCE_GTFS,
            None,
            None,
            legacy_key=train_natural_key(
                route.get("route_short_name"), route.get("agency_id"), first_dep, station_keys
            ),
        )
        train_key_by_trip[trip_id] = key
        if key in train_id_by_key or key in new_trains:
            continue
        pattern_by_key[key] = stops
        last_arr = next((s.arr for s in reversed(stops) if s.arr is not None), None)
        long_name = _train_name(route.get("route_short_name"), stops, children, parents)
        new_trains[key] = {
            "source": SOURCE_GTFS,
            "identity_kind": IDENTITY_PATTERN,
            "identity_key": key,
            "train_number": None,  # genuinely absent from gtfs.de feeds
            "long_name": long_name,
            "display_name": long_name,
            "search_name": search_norm(long_name)[:64],
            "route_id": route_pk_by_gtfs.get(route_gtfs_id),
            "segment": segment_of(route.get("route_short_name"), None),
            "origin_station_id": station_id_by_stop.get(stops[0].stop_id),
            "dest_station_id": station_id_by_stop.get(stops[-1].stop_id),
            "first_departure_sec": first_dep,
            "last_arrival_sec": last_arr,
            "n_stops": len(stops),
            "first_seen_feed_version_id": fv_id,
            "last_seen_feed_version_id": fv_id,
        }

    if new_trains:
        session.execute(insert(Train), list(new_trains.values()))
        session.flush()
    stats.trains = len(new_trains)
    train_id_by_key = dict(session.execute(select(Train.identity_key, Train.id)).all())

    # --- station_stops (the planned pattern, once per train per feed version) ---
    progress("  writing station stops ...")
    seen_train_ids: set[int] = set()
    ss_rows = []
    for trip_id, stops in by_trip.items():
        key = train_key_by_trip.get(trip_id)
        if key is None:
            continue
        train_id = train_id_by_key[key]
        if train_id in seen_train_ids:
            continue
        seen_train_ids.add(train_id)
        for stop in stops:
            station_id = station_id_by_stop.get(stop.stop_id)
            if station_id is None:
                continue
            ss_rows.append(
                {
                    "feed_version_id": fv_id,
                    "train_id": train_id,
                    # The feed's own stop_sequence, never renumbered: realtime
                    # stop_time_updates are keyed by it.
                    "stop_sequence": stop.seq,
                    "station_id": station_id,
                    "source_stop_id": stop.stop_id,
                    "platform_code": platform_by_stop.get(stop.stop_id),
                    "planned_arrival_sec": stop.arr,
                    "planned_departure_sec": stop.dep,
                    "pickup_type": stop.pickup,
                    "drop_off_type": stop.dropoff,
                }
            )
    _bulk(session, StationStop, ss_rows)
    stats.station_stops = len(ss_rows)

    # --- calendar ---------------------------------------------------------
    # Read for its date range only. The dated-run expansion that used to live here
    # wrote 1.27M `trip` rows plus an identical `trip_service_date` copy — 408 MB,
    # a third of it byte-for-byte duplication — for a source that never joins to a
    # recorded actual. `trip` now holds IRIS runs only.
    progress("  reading calendar ...")
    dates_by_service = service_dates(zf)
    all_dates = {d for dates in dates_by_service.values() for d in dates}

    if seen_train_ids:
        ids = list(seen_train_ids)
        for start in range(0, len(ids), 500):
            session.execute(
                Train.__table__.update()
                .where(Train.id.in_(ids[start : start + 500]))
                .values(last_seen_feed_version_id=fv_id)
            )

    feed_version.status = "active"
    feed_version.imported_at = int(time.time())
    feed_version.n_trips = stats.trains
    feed_version.n_stops = stats.stations
    feed_version.n_station_stops = stats.station_stops
    feed_version.feed_start_date = min(all_dates) if all_dates else None
    feed_version.feed_end_date = max(all_dates) if all_dates else None

    # Only one version per kind is current; older ones stay for provenance.
    session.execute(
        FeedVersion.__table__.update()
        .where(
            FeedVersion.kind == kind,
            FeedVersion.id != fv_id,
            FeedVersion.status == "active",
        )
        .values(status="superseded")
    )

    stats.elapsed_s = time.monotonic() - started
    return stats


def key_for_stop(stop_id: str, station_id_by_stop: dict[str, int]) -> str:
    """Stable string for a stop, used when hashing a train's pattern."""
    return str(station_id_by_stop.get(stop_id, f"?{stop_id}"))


def _train_name(
    product: str | None,
    stops: list[_StopTime],
    children: dict[str, dict[str, str]],
    parents: dict[str, dict[str, str]],
) -> str:
    def name_of(stop_id: str) -> str:
        row = children.get(stop_id) or parents.get(stop_id)
        return row["stop_name"] if row else stop_id

    headsign = next((s.headsign for s in stops if s.headsign), None)
    destination = headsign or name_of(stops[-1].stop_id)
    return f"{product or '?'} {name_of(stops[0].stop_id)} → {destination}"


def _bulk(session: Session, model, rows: list[dict], chunk: int = 5000) -> None:
    for start in range(0, len(rows), chunk):
        session.execute(insert(model), rows[start : start + chunk])
    session.flush()


def _purge_feed_version(session: Session, fv_id: int) -> None:
    """Remove rows produced by a GTFS feed version, newest dependency first.

    Only ever a GTFS version: the Timetables feed_version row is synthetic and is
    never imported through here, so no recorded actual is reachable from this.
    """
    session.execute(delete(StationStop).where(StationStop.feed_version_id == fv_id))
    session.execute(delete(Route).where(Route.feed_version_id == fv_id))
    session.flush()
