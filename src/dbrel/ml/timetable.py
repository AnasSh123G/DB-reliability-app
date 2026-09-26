"""Raw feature inputs for a scheduled call, read from the timetable alone.

This is the prediction side of `features.RAW_COLUMNS`. It reads `train`,
`station_stop`, `station` and the holiday calendar -- never `trip_stop`, `trip`
or either averages table. That is what makes a prediction "offline": it needs
the saved model and the timetable, not a history of what trains did.

Position on the route is the stop's 0-based rank among the station_stop rows of
its route variant (feed version, train, path_sha1), ordered by stop_sequence --
the same window the dataset builder's SQL computes. stop_sequence itself is
the sparse IRIS stopIndex and is not comparable across variants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.holidays_de import is_holiday
from dbrel.ml.features import NO_LINE, RAW_COLUMNS, weekday_of
from dbrel.models import Station, StationStop, Train


@dataclass(frozen=True)
class ScheduledCall:
    """One call of one train at one station on one service date."""

    train: Train
    station: Station
    service_date: date
    planned_arrival_sec: int | None  # service-day seconds, may exceed 86400
    planned_departure_sec: int | None
    stop_rank: int
    n_stops: int

    def raw(self, *, direction: int, holiday_set: set[tuple[str, str]],
            arrival_clock: bool = False) -> dict[str, list]:
        """This call as a one-row `RAW_COLUMNS` dict.

        `arrival_clock`: the planned arrival as the scheduled time (arrival-only
        models), instead of the departure-preferred time the v2 model uses.
        """
        train, station = self.train, self.station
        if arrival_clock:
            sched = self.planned_arrival_sec
        else:
            sched = self.planned_departure_sec
            if sched is None:
                sched = self.planned_arrival_sec
        start = train.first_departure_sec
        row = {
            "station_id": station.id,
            "train_id": train.id,
            "line_id": train.line_id if train.line_id is not None else NO_LINE,
            "category": train.category,
            "operator": train.operator,
            "state": station.state,
            "lat": station.lat,
            "lon": station.lon,
            "weekday": weekday_of(self.service_date),
            "is_holiday": int(is_holiday(self.service_date, station.state, holiday_set)),
            "sched_local_sec": sched,
            "trip_start_sec": np.nan if start is None else start,
            "stop_rank": self.stop_rank,
            "n_stops": self.n_stops,
            "direction": direction,
        }
        assert tuple(row) == RAW_COLUMNS
        return {key: [value] for key, value in row.items()}


def calls_at(session: Session, train: Train, station_id: int, service_date: date) -> list[ScheduledCall]:
    """Every call `train` makes at `station_id`, on its main route variant if it has one.

    Usually one call; a train that passes a station twice (a loop) gives two.
    Empty when the timetable has no such stop.
    """
    rows = session.execute(
        select(StationStop)
        .where(StationStop.train_id == train.id)
        .order_by(StationStop.feed_version_id, StationStop.path_sha1, StationStop.stop_sequence)
    ).scalars().all()
    if not rows:
        return []
    variants: dict[tuple, list[StationStop]] = {}
    for row in rows:
        variants.setdefault((row.feed_version_id, row.path_sha1), []).append(row)
    main = [key for key in variants if key[1] == train.path_sha1]
    ordered_keys = main + [key for key in variants if key not in main]

    station = session.get(Station, station_id)
    if station is None:
        return []
    for key in ordered_keys:
        stops = variants[key]
        calls = [
            ScheduledCall(
                train=train,
                station=station,
                service_date=service_date,
                planned_arrival_sec=stop.planned_arrival_sec,
                planned_departure_sec=stop.planned_departure_sec,
                stop_rank=rank,
                n_stops=len(stops),
            )
            for rank, stop in enumerate(stops)
            if stop.station_id == station_id
        ]
        if calls:
            return calls
    return []
