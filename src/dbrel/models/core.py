"""The core domain: stations, trains, their planned patterns, and dated runs.

Station / Train / StationStop are the tables behind the original dataclasses in
main.py. Trip and TripStop add the dated dimension the dataclasses lacked: a train
is a timetable, a trip is one particular run of it on one particular day, and only
a run can have an actual time.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dbrel.db import Base

# Quality grades for a derived actual time. See derive/finalize.py.
QUALITY_OBSERVED = "OBSERVED"
QUALITY_INFERRED_DOWNSTREAM = "INFERRED_DOWNSTREAM"
QUALITY_LAST_SEEN = "LAST_SEEN"
QUALITY_PREDICTION_ONLY = "PREDICTION_ONLY"
QUALITY_SKIPPED = "SKIPPED"
QUALITY_NO_DATA = "NO_DATA"
QUALITY_MISSING = "MISSING"

TRUSTED_QUALITIES = (QUALITY_OBSERVED, QUALITY_INFERRED_DOWNSTREAM, QUALITY_LAST_SEEN)


class Station(Base):
    """A station, stable across feed versions.

    gtfs.de stop_ids are opaque integers, not IFOPT/DHIDs, and may be re-minted when
    the feed is regenerated. Identity is therefore carried by natural_key
    (normalised name + coordinates rounded to ~110 m), which was verified unique
    across all 7,652 rv parent stations. See docs/probe-findings.md.

    Only parent stations (location_type=1) become rows here; platforms are recorded
    as platform_code on the stop that references them.
    """

    __tablename__ = "station"

    id: Mapped[int] = mapped_column(primary_key=True)
    natural_key: Mapped[str] = mapped_column(String(160), unique=True)
    name: Mapped[str] = mapped_column(Text)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    first_seen_feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))

    station_stops: Mapped[list["StationStop"]] = relationship(back_populates="station")

    __table_args__ = (Index("ix_station_name", "name"),)

    def __repr__(self) -> str:
        return f"<Station {self.id} {self.name!r}>"


class Train(Base):
    """A train as a recurring service: 'the 08:35 Singen-Stuttgart', not one run.

    This is the identity that has to survive weekly feed rebuilds, so it is keyed on
    natural_key (route + headsign + endpoints + departure time) rather than on any
    GTFS id.
    """

    __tablename__ = "train"

    id: Mapped[int] = mapped_column(primary_key=True)
    natural_key: Mapped[str] = mapped_column(String(255), unique=True)
    train_number: Mapped[str | None] = mapped_column(String(32))
    train_name: Mapped[str | None] = mapped_column(Text)
    route_id: Mapped[int | None] = mapped_column(ForeignKey("route.id"), index=True)
    route_type: Mapped[int | None] = mapped_column(Integer)
    origin_station_id: Mapped[int | None] = mapped_column(ForeignKey("station.id"))
    dest_station_id: Mapped[int | None] = mapped_column(ForeignKey("station.id"))
    first_departure_sec: Mapped[int | None] = mapped_column(Integer)
    last_arrival_sec: Mapped[int | None] = mapped_column(Integer)
    n_stops: Mapped[int | None] = mapped_column(Integer)
    first_seen_feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))
    last_seen_feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))

    route: Mapped["Route | None"] = relationship(back_populates="trains")  # noqa: F821
    origin: Mapped["Station | None"] = relationship(foreign_keys=[origin_station_id])
    destination: Mapped["Station | None"] = relationship(foreign_keys=[dest_station_id])
    path: Mapped[list["StationStop"]] = relationship(
        back_populates="train", order_by="StationStop.stop_sequence"
    )
    trips: Mapped[list["Trip"]] = relationship(back_populates="train")

    __table_args__ = (Index("ix_train_number", "train_number"),)

    def __repr__(self) -> str:
        return f"<Train {self.id} {self.train_number or self.train_name!r}>"


class StationStop(Base):
    """One stop in a train's planned pattern: where, in what order, at what time.

    Date-independent, so times are seconds from the service-date anchor rather than
    absolute instants — that is what makes 25:30:00 representable. Carrying
    feed_version_id means a schedule change between rebuilds is visible as a new row
    rather than mistaken for corruption.
    """

    __tablename__ = "station_stop"

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_version_id: Mapped[int] = mapped_column(ForeignKey("feed_version.id"), index=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("train.id"))
    stop_sequence: Mapped[int] = mapped_column(Integer)
    station_id: Mapped[int] = mapped_column(ForeignKey("station.id"))
    gtfs_stop_id: Mapped[str] = mapped_column(String(64))
    platform_code: Mapped[str | None] = mapped_column(String(32))
    planned_arrival_sec: Mapped[int | None] = mapped_column(Integer)
    planned_departure_sec: Mapped[int | None] = mapped_column(Integer)
    pickup_type: Mapped[int | None] = mapped_column(Integer)
    drop_off_type: Mapped[int | None] = mapped_column(Integer)

    train: Mapped["Train"] = relationship(back_populates="path")
    station: Mapped["Station"] = relationship(back_populates="station_stops")

    __table_args__ = (
        UniqueConstraint("feed_version_id", "train_id", "stop_sequence", name="uq_ss_fv_train_seq"),
        Index("ix_ss_station", "station_id"),
        Index("ix_ss_train", "train_id"),
    )

    def __repr__(self) -> str:
        return f"<StationStop train={self.train_id} seq={self.stop_sequence} station={self.station_id}>"


class Trip(Base):
    """One dated run of a train. Links train to its stops and carries journey timing.

    Rows exist for every scheduled run, not only observed ones — a train that never
    appears in the realtime feed is finalised as MISSING, which is a fact worth
    recording rather than an absence.
    """

    __tablename__ = "trip"

    id: Mapped[int] = mapped_column(primary_key=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("train.id"))
    service_date: Mapped[str] = mapped_column(String(10))
    feed_version_id: Mapped[int] = mapped_column(ForeignKey("feed_version.id"))
    gtfs_trip_id: Mapped[str] = mapped_column(String(64))

    # SCHEDULED | RUNNING | COMPLETED | CANCELLED | MISSING
    status: Mapped[str] = mapped_column(String(16), default="SCHEDULED")

    supposed_departure_utc: Mapped[int | None] = mapped_column(Integer)
    actual_departure_utc: Mapped[int | None] = mapped_column(Integer)
    supposed_arrival_utc: Mapped[int | None] = mapped_column(Integer)
    actual_arrival_utc: Mapped[int | None] = mapped_column(Integer)

    final_delay_s: Mapped[int | None] = mapped_column(Integer)
    max_delay_s: Mapped[int | None] = mapped_column(Integer)
    n_stops: Mapped[int | None] = mapped_column(Integer)
    n_stops_observed: Mapped[int | None] = mapped_column(Integer)
    n_polls_seen: Mapped[int] = mapped_column(Integer, default=0)
    quality: Mapped[str | None] = mapped_column(String(24))
    rule_version: Mapped[int | None] = mapped_column(Integer)
    finalized_at: Mapped[int | None] = mapped_column(Integer)

    train: Mapped["Train"] = relationship(back_populates="trips")
    stops: Mapped[list["TripStop"]] = relationship(
        back_populates="trip", order_by="TripStop.stop_sequence"
    )

    __table_args__ = (
        UniqueConstraint("train_id", "service_date", name="uq_trip_train_date"),
        # The realtime resolver's hot path: descriptors carry only trip_id + start_date.
        Index("ix_trip_gtfsid_date", "gtfs_trip_id", "service_date"),
        Index("ix_trip_date_status", "service_date", "status"),
        Index("ix_trip_date_delay", "service_date", "final_delay_s"),
    )

    def __repr__(self) -> str:
        return f"<Trip {self.id} train={self.train_id} {self.service_date} {self.status}>"


class TripStop(Base):
    """Supposed vs actual, for one run at one station. The fact table.

    Supposed times are copied here rather than joined from StationStop: the planned
    time of a run that already happened is a historical fact, and must not change
    when next week's feed rebuild alters the pattern.
    """

    __tablename__ = "trip_stop"

    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trip.id"))
    stop_sequence: Mapped[int] = mapped_column(Integer)
    station_id: Mapped[int] = mapped_column(ForeignKey("station.id"))
    station_stop_id: Mapped[int | None] = mapped_column(ForeignKey("station_stop.id"))

    supposed_arrival_utc: Mapped[int | None] = mapped_column(Integer)
    supposed_departure_utc: Mapped[int | None] = mapped_column(Integer)
    actual_arrival_utc: Mapped[int | None] = mapped_column(Integer)
    actual_departure_utc: Mapped[int | None] = mapped_column(Integer)
    arr_delay_s: Mapped[int | None] = mapped_column(Integer)
    dep_delay_s: Mapped[int | None] = mapped_column(Integer)

    quality: Mapped[str] = mapped_column(String(24))
    rule_version: Mapped[int] = mapped_column(Integer)
    n_observations: Mapped[int] = mapped_column(Integer, default=0)
    last_observed_at: Mapped[int | None] = mapped_column(Integer)
    poll_gap_s: Mapped[int | None] = mapped_column(Integer)
    cancelled: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    # Local hour of the supposed departure, precomputed for hour-of-day aggregation.
    supposed_departure_hour: Mapped[int | None] = mapped_column(Integer)

    trip: Mapped["Trip"] = relationship(back_populates="stops")
    station: Mapped["Station"] = relationship()

    __table_args__ = (
        UniqueConstraint("trip_id", "stop_sequence", name="uq_tripstop_trip_seq"),
        # Each index ends in the measure so aggregations are answered from the index.
        Index("ix_ts_station_time_delay", "station_id", "supposed_departure_utc", "dep_delay_s"),
        Index("ix_ts_hour_delay", "supposed_departure_hour", "dep_delay_s"),
        Index("ix_ts_quality", "quality"),
    )

    def __repr__(self) -> str:
        return f"<TripStop trip={self.trip_id} seq={self.stop_sequence} dep_delay={self.dep_delay_s}>"
