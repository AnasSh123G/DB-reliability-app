"""GTFS-Realtime protobuf -> plain dataclasses.

Pure: takes bytes, returns data, touches no database. The feed covers all of Germany
including buses and trams, ~92% of which is out of scope here, so `keep` filters trips
before any object is constructed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from google.transit import gtfs_realtime_pb2

# StopTimeUpdate.ScheduleRelationship
STU_SCHEDULED = 0
STU_SKIPPED = 1
STU_NO_DATA = 2

# TripDescriptor.ScheduleRelationship
TRIP_SCHEDULED = 0
TRIP_ADDED = 1
TRIP_UNSCHEDULED = 2
TRIP_CANCELED = 3


@dataclass(slots=True)
class StopUpdate:
    stop_sequence: int
    stop_id: str
    arr_delay_s: int | None
    arr_time_utc: int | None
    dep_delay_s: int | None
    dep_time_utc: int | None
    schedule_relationship: int

    def value(self) -> tuple:
        """The tuple compared against the last stored observation."""
        return (
            self.arr_delay_s,
            self.dep_delay_s,
            self.arr_time_utc,
            self.dep_time_utc,
            self.schedule_relationship,
        )


@dataclass(slots=True)
class TripUpdate:
    trip_id: str
    start_date: str
    start_time: str
    route_id: str
    schedule_relationship: int
    timestamp: int | None
    stops: list[StopUpdate] = field(default_factory=list)


@dataclass(slots=True)
class ParsedFeed:
    timestamp: int
    n_entities: int
    n_trip_updates: int
    n_alerts: int
    trips: list[TripUpdate]
    parse_ms: int

    @property
    def n_stop_updates(self) -> int:
        return sum(len(t.stops) for t in self.trips)


def parse_feed(data: bytes, keep: Callable[[str], bool] | None = None) -> ParsedFeed:
    """Parse a GTFS-RT FeedMessage.

    `keep` is called with each trip_id; return False to discard it before its stop
    updates are read. Filtering here rather than later is what keeps a 47 MB message
    cheap to process.
    """
    started = time.monotonic()
    message = gtfs_realtime_pb2.FeedMessage()
    message.ParseFromString(data)

    trips: list[TripUpdate] = []
    n_trip_updates = n_alerts = 0

    for entity in message.entity:
        if entity.HasField("alert"):
            n_alerts += 1
        if not entity.HasField("trip_update"):
            continue
        n_trip_updates += 1
        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        if not trip_id or (keep is not None and not keep(trip_id)):
            continue

        stops: list[StopUpdate] = []
        for stu in tu.stop_time_update:
            arrival = stu.arrival if stu.HasField("arrival") else None
            departure = stu.departure if stu.HasField("departure") else None
            stops.append(
                StopUpdate(
                    stop_sequence=stu.stop_sequence,
                    stop_id=stu.stop_id,
                    # The spec gives `time` precedence over `delay`; in this feed both
                    # are always populated together, so they should agree anyway.
                    arr_delay_s=arrival.delay if arrival is not None and arrival.HasField("delay") else None,
                    arr_time_utc=arrival.time if arrival is not None and arrival.HasField("time") else None,
                    dep_delay_s=departure.delay if departure is not None and departure.HasField("delay") else None,
                    dep_time_utc=departure.time if departure is not None and departure.HasField("time") else None,
                    schedule_relationship=stu.schedule_relationship,
                )
            )

        trips.append(
            TripUpdate(
                trip_id=trip_id,
                start_date=tu.trip.start_date,
                start_time=tu.trip.start_time,
                route_id=tu.trip.route_id,
                schedule_relationship=tu.trip.schedule_relationship,
                timestamp=tu.timestamp or None,
                stops=stops,
            )
        )

    return ParsedFeed(
        timestamp=message.header.timestamp or int(time.time()),
        n_entities=len(message.entity),
        n_trip_updates=n_trip_updates,
        n_alerts=n_alerts,
        trips=trips,
        parse_ms=int((time.monotonic() - started) * 1000),
    )
