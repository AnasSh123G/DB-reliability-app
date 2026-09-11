"""The core domain: stations, trains, their planned patterns, and dated runs.

Station / Train / StationStop are the tables behind the original dataclasses in
main.py. Trip and TripStop add the dated dimension the dataclasses lacked: a train
is a timetable, a trip is one particular run of it on one particular day, and only
a run can have an actual time.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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

# Timetables-only grade. IRIS states a cancellation outright via cs='c' + clt,
# so this is measured rather than inferred — unlike SKIPPED, which is the
# GTFS-RT inference and has different provenance.
QUALITY_CANCELLED = "CANCELLED"

# Timetables-only, and the archival path's normal outcome. Read when the event
# was already in the past, so ct is DB's recorded actual rather than a forecast.
# Distinct from OBSERVED, which asserts something stronger and different — that a
# value was seen being revised after passage and then holding while we kept
# polling. A single late read cannot witness that, and must not claim it.
QUALITY_RECORDED = "RECORDED"

# A PLAN_HELD grade was designed for the case where /fchg published only changes,
# so that a punctual train never appeared and its silence had to be read as "ran
# to plan". Measurement killed it: 100% of planned events do appear, 97% carrying
# a ct, and punctual running is published explicitly as ct == pt
# (docs/probe-findings.md §18). Absence therefore still means absence.
TRUSTED_QUALITIES = (
    QUALITY_RECORDED,
    QUALITY_OBSERVED,
    QUALITY_INFERRED_DOWNSTREAM,
    QUALITY_LAST_SEEN,
)


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

    # IRIS identity. A property of the place, so it lives here rather than on
    # tracked_station: untracked stations need it too, because it is the only
    # reliable join between the two sources — station names disagree
    # systematically between IRIS and DELFI (docs/probe-findings.md §13).
    eva: Mapped[int | None] = mapped_column(Integer)
    ds100: Mapped[str | None] = mapped_column(String(16))
    eva_source: Mapped[str | None] = mapped_column(String(16))  # iris | stada | manual
    eva_confidence: Mapped[float | None] = mapped_column(Float)  # km between coordinates
    eva_verified_at: Mapped[int | None] = mapped_column(Integer)

    station_stops: Mapped[list["StationStop"]] = relationship(back_populates="station")

    __table_args__ = (
        Index("ix_station_name", "name"),
        Index("ix_station_eva", "eva"),
    )

    def __repr__(self) -> str:
        return f"<Station {self.id} {self.name!r}>"


class ServiceLine(Base):
    """'RE 4', 'S1' — the thing a regional passenger actually names.

    A line has many numbered trains, so it cannot share a table with Train:
    emitting 's1' as an alias of a train would make that one query return every
    S1 run ever recorded. The split falls exactly on the FV/RV boundary — every
    regional stop carries dp@l and no long-distance one does (findings §11).
    """

    __tablename__ = "service_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    identity_key: Mapped[str] = mapped_column(String(64), unique=True)  # 'RE/4'
    category: Mapped[str] = mapped_column(String(8))
    line: Mapped[str | None] = mapped_column(String(16))
    operator: Mapped[str | None] = mapped_column(String(16))
    segment: Mapped[str | None] = mapped_column(String(4))
    display_name: Mapped[str] = mapped_column(String(32))
    search_name: Mapped[str] = mapped_column(String(32))
    n_trains: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_date: Mapped[str | None] = mapped_column(String(10))
    last_seen_date: Mapped[str | None] = mapped_column(String(10))

    __table_args__ = (Index("ix_line_cat_line", "category", "line"),)

    def __repr__(self) -> str:
        return f"<ServiceLine {self.id} {self.display_name!r}>"


class Train(Base):
    """A train as a recurring service, identified the way the railway identifies it.

    Under GTFS there was no train number in the feed at all, so identity had to be
    synthesised from the observed station sequence. That scheme cannot survive a
    station-scoped source: growing the tracked set would change the hash of every
    train through the new stations and fork the whole history. IRIS supplies the
    number, and (category, number) had zero collisions over 1,108 pairs, so the
    path is now an attribute and the number is the identity (findings §10).
    """

    __tablename__ = "train"

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- identity ---------------------------------------------------------
    source: Mapped[str] = mapped_column(String(8), default="gtfs")
    identity_kind: Mapped[str] = mapped_column(String(12), default="PATTERN")
    identity_key: Mapped[str] = mapped_column(String(255), unique=True)

    # --- the ids the backend keeps ----------------------------------------
    train_number: Mapped[int | None] = mapped_column(Integer)
    train_number_raw: Mapped[str | None] = mapped_column(String(16))
    category: Mapped[str | None] = mapped_column(String(8))
    operator: Mapped[str | None] = mapped_column(String(16))  # tl@o, attribute not key
    trip_type: Mapped[str | None] = mapped_column(String(2))  # tl@t verbatim
    segment: Mapped[str | None] = mapped_column(String(4))  # FV | RV | S
    line_id: Mapped[int | None] = mapped_column(ForeignKey("service_line.id"), index=True)

    # --- what a human types and reads -------------------------------------
    display_name: Mapped[str | None] = mapped_column(String(120))  # 'ICE 599'
    search_name: Mapped[str | None] = mapped_column(String(64))  # 'ice599'
    # IRIS fb: what the passenger is shown. For long distance that is the
    # number ("ICE 843"), for regional it is the LINE ("RE3"), so it must not
    # become display_name or a train would answer to its line's name.
    passenger_label: Mapped[str | None] = mapped_column(String(32))
    long_name: Mapped[str | None] = mapped_column(Text)  # descriptive, GTFS-era

    # --- shape: attributes, never identity --------------------------------
    origin_station_id: Mapped[int | None] = mapped_column(ForeignKey("station.id"))
    dest_station_id: Mapped[int | None] = mapped_column(ForeignKey("station.id"))
    # ppth names often do not resolve to a station row, so keep the text too.
    origin_name: Mapped[str | None] = mapped_column(Text)
    dest_name: Mapped[str | None] = mapped_column(Text)
    route_path: Mapped[str | None] = mapped_column(Text)  # pipe-separated ppth
    path_sha1: Mapped[str | None] = mapped_column(String(40))
    first_departure_sec: Mapped[int | None] = mapped_column(Integer)
    last_arrival_sec: Mapped[int | None] = mapped_column(Integer)
    n_stops: Mapped[int | None] = mapped_column(Integer)  # true journey length
    n_stops_tracked: Mapped[int | None] = mapped_column(Integer)  # how many we can see

    # --- lifecycle ---------------------------------------------------------
    first_seen_date: Mapped[str | None] = mapped_column(String(10))
    last_seen_date: Mapped[str | None] = mapped_column(String(10))
    n_runs_seen: Mapped[int] = mapped_column(Integer, default=0)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("train.id"))

    # --- GTFS provenance, unchanged ---------------------------------------
    route_id: Mapped[int | None] = mapped_column(ForeignKey("route.id"), index=True)
    first_seen_feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))
    last_seen_feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))

    route: Mapped["Route | None"] = relationship(back_populates="trains")  # noqa: F821
    line: Mapped["ServiceLine | None"] = relationship()
    origin: Mapped["Station | None"] = relationship(foreign_keys=[origin_station_id])
    destination: Mapped["Station | None"] = relationship(foreign_keys=[dest_station_id])
    path: Mapped[list["StationStop"]] = relationship(
        back_populates="train", order_by="StationStop.stop_sequence"
    )
    trips: Mapped[list["Trip"]] = relationship(back_populates="train")

    __table_args__ = (
        Index("ix_train_number", "train_number"),
        Index("ix_train_cat_number", "category", "train_number"),
        Index("ix_train_line", "line_id", "train_number"),
        Index("ix_train_search_name", "search_name"),
        Index("ix_train_source_kind", "source", "identity_kind"),
        Index("ix_train_merged", "merged_into_id"),
    )

    def __repr__(self) -> str:
        return f"<Train {self.id} {self.display_name or self.long_name!r}>"


class SearchAlias(Base):
    """Every string form a human might type, for any searchable entity.

    Three nullable FKs plus a CHECK rather than a polymorphic (kind, id) pair,
    because db.py sets PRAGMA foreign_keys=ON and this keeps deletes cascading.
    """

    __tablename__ = "search_alias"

    id: Mapped[int] = mapped_column(primary_key=True)
    alias_norm: Mapped[str] = mapped_column(String(96))
    alias_display: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(12))
    weight: Mapped[int] = mapped_column(Integer, default=100)
    train_id: Mapped[int | None] = mapped_column(ForeignKey("train.id", ondelete="CASCADE"))
    line_id: Mapped[int | None] = mapped_column(ForeignKey("service_line.id", ondelete="CASCADE"))
    station_id: Mapped[int | None] = mapped_column(ForeignKey("station.id", ondelete="CASCADE"))

    __table_args__ = (
        CheckConstraint(
            "(train_id IS NOT NULL) + (line_id IS NOT NULL) + (station_id IS NOT NULL) = 1",
            name="ck_alias_one_target",
        ),
        UniqueConstraint(
            "alias_norm", "train_id", "line_id", "station_id", name="uq_alias_norm_target"
        ),
        Index("ix_alias_lookup", "alias_norm", "weight"),
    )

    def __repr__(self) -> str:
        return f"<SearchAlias {self.alias_norm!r} {self.kind}>"


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
    # For Timetables this is the trip-wide 1-based stopIndex from the stop id, so
    # sequences are SPARSE (a run may have rows at 3, 11, 24) but true and
    # consistent across stations. Adding a station later fills a gap rather than
    # renumbering anything — which is what lets the tracked set grow.
    stop_sequence: Mapped[int] = mapped_column(Integer)
    station_id: Mapped[int] = mapped_column(ForeignKey("station.id"))
    source_stop_id: Mapped[str | None] = mapped_column(String(64))
    platform_code: Mapped[str | None] = mapped_column(String(32))
    planned_arrival_sec: Mapped[int | None] = mapped_column(Integer)
    planned_departure_sec: Mapped[int | None] = mapped_column(Integer)
    pickup_type: Mapped[int | None] = mapped_column(Integer)
    drop_off_type: Mapped[int | None] = mapped_column(Integer)

    train: Mapped["Train"] = relationship(back_populates="path")
    station: Mapped["Station"] = relationship(back_populates="station_stops")

    __table_args__ = (
        UniqueConstraint("feed_version_id", "train_id", "stop_sequence", name="uq_ss_fv_train_seq"),
        # station_id stays leading so a bare station lookup still seeks, but carrying
        # feed_version_id means a caller can scope to one feed rather than summing
        # every rebuild's copy of the same pattern.
        Index("ix_ss_station", "station_id", "feed_version_id", "planned_departure_sec"),
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
    # The planned departure date at the run's ORIGIN, never the local date of the
    # observed stop. IRIS carries it in every stop id, so the 23:50 overnight case
    # is unambiguous for the first time.
    service_date: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(8), default="gtfs")
    # GTFS: trip_id. Timetables: '<dailyTripId>-<YYMMddHHmm>', identical at every
    # station the run calls at — which is what stitches one run into one row.
    source_run_key: Mapped[str] = mapped_column(String(64))
    daily_trip_id: Mapped[str | None] = mapped_column(String(32))
    feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))

    # SCHEDULED | RUNNING | COMPLETED | CANCELLED | MISSING
    status: Mapped[str] = mapped_column(String(16), default="SCHEDULED")
    # PLANNED | ADDED | CANCELLED — from IRIS ps/cs, so punctuality stats can
    # exclude runs that were never in the plan.
    schedule_relationship: Mapped[str | None] = mapped_column(String(12))
    cancelled_at_utc: Mapped[int | None] = mapped_column(Integer)

    # For Timetables these are the first/last TRACKED supposed times, not the true
    # journey endpoints, which are usually at stations we do not watch.
    supposed_departure_utc: Mapped[int | None] = mapped_column(Integer)
    actual_departure_utc: Mapped[int | None] = mapped_column(Integer)
    supposed_arrival_utc: Mapped[int | None] = mapped_column(Integer)
    actual_arrival_utc: Mapped[int | None] = mapped_column(Integer)
    endpoints_tracked: Mapped[int] = mapped_column(Integer, default=0)

    final_delay_s: Mapped[int | None] = mapped_column(Integer)
    max_delay_s: Mapped[int | None] = mapped_column(Integer)
    n_stops: Mapped[int | None] = mapped_column(Integer)
    n_stops_tracked: Mapped[int | None] = mapped_column(Integer)
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
        # A numbered train runs once a day. Ingest routes a violation into
        # train_conflict rather than crashing or silently overwriting.
        UniqueConstraint("train_id", "service_date", name="uq_trip_train_date"),
        # The resolver's hot path, for both sources.
        UniqueConstraint("source", "source_run_key", "service_date", name="uq_trip_src_run_date"),
        Index("ix_trip_srcid_date", "source_run_key", "service_date"),
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
    # Seconds between the event happening and us reading its value. The confidence
    # measure for QUALITY_RECORDED, where poll_gap_s has nothing to say: a value
    # read long after passage had longer to stop moving.
    read_lag_s: Mapped[int | None] = mapped_column(Integer)
    cancelled: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)

    # --- Timetables specifics --------------------------------------------
    # A stop can be cancelled on one side only, which an int enum could not say.
    arr_cancelled: Mapped[int] = mapped_column(Integer, default=0)
    dep_cancelled: Mapped[int] = mapped_column(Integer, default=0)
    # clt: how far in advance the cancellation was published — a new metric.
    cancelled_at_utc: Mapped[int | None] = mapped_column(Integer)
    added: Mapped[int] = mapped_column(Integer, default=0)
    planned_platform: Mapped[str | None] = mapped_column(String(16))
    changed_platform: Mapped[str | None] = mapped_column(String(16))
    # Where a run changes number, one physical stop appears on both sides; a
    # station-level average that counts both would double-count it.
    is_handover: Mapped[int] = mapped_column(Integer, default=0)

    # Local hour of the supposed departure, precomputed for hour-of-day aggregation.
    supposed_departure_hour: Mapped[int | None] = mapped_column(Integer)

    trip: Mapped["Trip"] = relationship(back_populates="stops")
    station: Mapped["Station"] = relationship()

    __table_args__ = (
        UniqueConstraint("trip_id", "stop_sequence", name="uq_tripstop_trip_seq"),
        # Each index ends in the measure so aggregations are answered from the index,
        # and carries is_handover before it: a station-level average must exclude
        # handover stops, and reading that flag from the table would defeat the point.
        Index(
            "ix_ts_station_dep",
            "station_id",
            "supposed_departure_utc",
            "is_handover",
            "dep_delay_s",
        ),
        Index(
            "ix_ts_station_arr",
            "station_id",
            "supposed_arrival_utc",
            "is_handover",
            "arr_delay_s",
        ),
        Index("ix_ts_hour_delay", "supposed_departure_hour", "is_handover", "dep_delay_s"),
        Index("ix_ts_quality_delay", "quality", "dep_delay_s"),
        # Pattern row -> its dated instances. A nullable FK, so unindexed it forced
        # a full scan of the fact table.
        Index("ix_ts_station_stop", "station_stop_id"),
    )

    def __repr__(self) -> str:
        return f"<TripStop trip={self.trip_id} seq={self.stop_sequence} dep_delay={self.dep_delay_s}>"
