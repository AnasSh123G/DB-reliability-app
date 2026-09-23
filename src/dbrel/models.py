"""All ORM models and domain constants."""

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

# ---------------------------------------------------------------------------
# Day-type vocabulary
# ---------------------------------------------------------------------------

# Two buckets, not seven: a public holiday runs a Sunday timetable, so it
# belongs with the Sundays, and everything else pools together. Saturday counts
# as a weekday — it runs a thinner timetable but it is a working day.
# dbrel/holidays_de.py decides which is which.
DAY_TYPE_WEEKDAY = "WEEKDAY"
DAY_TYPE_SUNDAY_HOLIDAY = "SUNDAY_HOLIDAY"

# ---------------------------------------------------------------------------
# Quality constants
# ---------------------------------------------------------------------------

# The normal outcome. The event was already in the past when we read it, so ct is
# DB's recorded actual — the time DB Navigator shows after the train has gone —
# rather than a forecast. read_lag_s says how long after the event we read it.
QUALITY_RECORDED = "RECORDED"

# IRIS states a cancellation outright via cs='c' plus a clt timestamp, so this is
# measured, never inferred, and contributes no delay.
QUALITY_CANCELLED = "CANCELLED"

# We were polling this station across the window in which the value would still
# have been in /fchg's horizon, and nothing appeared. A fact about the railway.
# Never means "on time": punctual running is published explicitly as ct == pt
# (docs/probe-findings.md §18), so absence really is absence.
QUALITY_NO_DATA = "NO_DATA"

# No poll covered the window at all — our own blind spot, not the railway's
# silence. poll_gap_s records how wide it was.
QUALITY_PREDICTION_ONLY = "PREDICTION_ONLY"

# The grades an aggregate may compute a delay from.
TRUSTED_QUALITIES = (QUALITY_RECORDED,)

# ---------------------------------------------------------------------------
# Core domain: stations, trains, their planned patterns, and dated runs
# ---------------------------------------------------------------------------


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
    # ISO 3166-2:DE code ('DE-NW', ...), resolved once from (lat, lon) by
    # geo_states.state_for. NULL means the point fell outside all 16 state
    # boundaries (a cross-border stop, or unresolved) -- see holiday-calendar
    # spec's "station outside Germany" scenario.
    state: Mapped[str | None] = mapped_column(String(8))
    first_seen_feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))

    eva: Mapped[int | None] = mapped_column(Integer)
    ds100: Mapped[str | None] = mapped_column(String(16))
    eva_source: Mapped[str | None] = mapped_column(String(16))  # iris | stada | manual

    tracked: Mapped[int] = mapped_column(Integer, default=0)
    poll_priority: Mapped[int] = mapped_column(Integer, default=1)
    tracked_at: Mapped[int | None] = mapped_column(Integer)
    last_plan_hour: Mapped[str | None] = mapped_column(String(13))
    last_fchg_at: Mapped[int | None] = mapped_column(Integer)
    n_fchg_polls: Mapped[int] = mapped_column(Integer, default=0)
    n_fchg_errors: Mapped[int] = mapped_column(Integer, default=0)
    verify_overlap: Mapped[float | None] = mapped_column(Float)
    verified_at: Mapped[int | None] = mapped_column(Integer)

    station_stops: Mapped[list["StationStop"]] = relationship(back_populates="station")

    __table_args__ = (
        Index("ix_station_name", "name"),
        Index("ix_station_eva", "eva"),
        Index("ix_station_tracked", "tracked", "poll_priority"),
    )

    def __repr__(self) -> str:
        return f"<Station {self.id} {self.name!r}>"


class ServiceLine(Base):
    """'RE 4', 'S1' — the thing a regional passenger actually names."""

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
    """A train as a recurring service, identified the way the railway identifies it."""

    __tablename__ = "train"

    id: Mapped[int] = mapped_column(primary_key=True)

    source: Mapped[str] = mapped_column(String(8), default="gtfs")
    identity_kind: Mapped[str] = mapped_column(String(12), default="PATTERN")
    identity_key: Mapped[str] = mapped_column(String(255), unique=True)

    train_number: Mapped[int | None] = mapped_column(Integer)
    train_number_raw: Mapped[str | None] = mapped_column(String(16))
    category: Mapped[str | None] = mapped_column(String(8))
    operator: Mapped[str | None] = mapped_column(String(16))
    trip_type: Mapped[str | None] = mapped_column(String(2))
    segment: Mapped[str | None] = mapped_column(String(4))  # FV | RV | S
    line_id: Mapped[int | None] = mapped_column(ForeignKey("service_line.id"), index=True)

    display_name: Mapped[str | None] = mapped_column(String(120))  # 'ICE 599'
    search_name: Mapped[str | None] = mapped_column(String(64))  # 'ice599'
    passenger_label: Mapped[str | None] = mapped_column(String(32))
    long_name: Mapped[str | None] = mapped_column(Text)

    origin_station_id: Mapped[int | None] = mapped_column(ForeignKey("station.id"))
    dest_station_id: Mapped[int | None] = mapped_column(ForeignKey("station.id"))
    origin_name: Mapped[str | None] = mapped_column(Text)
    dest_name: Mapped[str | None] = mapped_column(Text)
    route_path: Mapped[str | None] = mapped_column(Text)  # pipe-separated ppth
    path_sha1: Mapped[str | None] = mapped_column(String(40))
    first_departure_sec: Mapped[int | None] = mapped_column(Integer)
    last_arrival_sec: Mapped[int | None] = mapped_column(Integer)
    n_stops: Mapped[int | None] = mapped_column(Integer)
    n_stops_tracked: Mapped[int | None] = mapped_column(Integer)

    first_seen_date: Mapped[str | None] = mapped_column(String(10))
    last_seen_date: Mapped[str | None] = mapped_column(String(10))
    n_runs_seen: Mapped[int] = mapped_column(Integer, default=0)

    route_id: Mapped[int | None] = mapped_column(ForeignKey("route.id"), index=True)
    first_seen_feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))
    last_seen_feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))

    route: Mapped["Route | None"] = relationship(back_populates="trains")
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
    )

    def __repr__(self) -> str:
        return f"<Train {self.id} {self.display_name or self.long_name!r}>"


class SearchAlias(Base):
    """Every string form a human might type, for any searchable entity."""

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
    """One stop in a train's planned pattern: where, in what order, at what time."""

    __tablename__ = "station_stop"

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_version_id: Mapped[int] = mapped_column(ForeignKey("feed_version.id"), index=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("train.id"))
    stop_sequence: Mapped[int] = mapped_column(Integer)
    station_id: Mapped[int] = mapped_column(ForeignKey("station.id"))
    source_stop_id: Mapped[str | None] = mapped_column(String(64))
    platform_code: Mapped[str | None] = mapped_column(String(32))
    planned_arrival_sec: Mapped[int | None] = mapped_column(Integer)
    planned_departure_sec: Mapped[int | None] = mapped_column(Integer)
    pickup_type: Mapped[int | None] = mapped_column(Integer)
    drop_off_type: Mapped[int | None] = mapped_column(Integer)
    # Which route VARIANT this stop belongs to (see Trip.path_sha1). A numbered
    # TT train's identity is (category, number) alone -- it does not fork when
    # the same number runs a different physical route on a different day (a
    # wing/coupled service is the common case), so stop_sequence's meaning is
    # only stable WITHIN one path_sha1. NULL for pre-migration/GTFS rows, which
    # keep the old (train_id, stop_sequence) behaviour.
    path_sha1: Mapped[str | None] = mapped_column(String(40))

    train: Mapped["Train"] = relationship(back_populates="path")
    station: Mapped["Station"] = relationship(back_populates="station_stops")

    __table_args__ = (
        UniqueConstraint(
            "feed_version_id", "train_id", "path_sha1", "stop_sequence",
            name="uq_ss_fv_train_variant_seq",
        ),
        Index("ix_ss_station", "station_id", "feed_version_id", "planned_departure_sec"),
        Index("ix_ss_station_arr", "station_id", "feed_version_id", "planned_arrival_sec"),
        Index("ix_ss_train", "train_id"),
        Index("ix_ss_train_variant", "train_id", "path_sha1"),
    )

    def __repr__(self) -> str:
        return f"<StationStop train={self.train_id} seq={self.stop_sequence} station={self.station_id}>"


class Trip(Base):
    """One dated run of a train."""

    __tablename__ = "trip"

    id: Mapped[int] = mapped_column(primary_key=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("train.id"))
    service_date: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(8), default="gtfs")
    source_run_key: Mapped[str] = mapped_column(String(64))
    daily_trip_id: Mapped[str | None] = mapped_column(String(32))
    feed_version_id: Mapped[int | None] = mapped_column(ForeignKey("feed_version.id"))

    status: Mapped[str] = mapped_column(String(16), default="SCHEDULED")
    schedule_relationship: Mapped[str | None] = mapped_column(String(12))
    cancelled_at_utc: Mapped[int | None] = mapped_column(Integer)

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
    # This run's own path_sha1 (runs.py upsert_run) -- which station_stop
    # variant its stop_sequence numbers refer to. See StationStop.path_sha1.
    path_sha1: Mapped[str | None] = mapped_column(String(40))

    train: Mapped["Train"] = relationship(back_populates="trips")
    stops: Mapped[list["TripStop"]] = relationship(
        back_populates="trip", order_by="TripStop.stop_sequence"
    )

    __table_args__ = (
        UniqueConstraint("train_id", "service_date", name="uq_trip_train_date"),
        UniqueConstraint("source", "source_run_key", "service_date", name="uq_trip_src_run_date"),
        Index("ix_trip_srcid_date", "source_run_key", "service_date"),
        Index("ix_trip_date_status", "service_date", "status"),
        Index("ix_trip_date_delay", "service_date", "final_delay_s"),
    )

    def __repr__(self) -> str:
        return f"<Trip {self.id} train={self.train_id} {self.service_date} {self.status}>"


class TripStop(Base):
    """Supposed vs actual, for one run at one station. The fact table."""

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
    last_observed_at: Mapped[int | None] = mapped_column(Integer)
    poll_gap_s: Mapped[int | None] = mapped_column(Integer)
    read_lag_s: Mapped[int | None] = mapped_column(Integer)
    cancelled: Mapped[int] = mapped_column(Integer, default=0)

    arr_cancelled: Mapped[int] = mapped_column(Integer, default=0)
    dep_cancelled: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_at_utc: Mapped[int | None] = mapped_column(Integer)
    added: Mapped[int] = mapped_column(Integer, default=0)
    planned_platform: Mapped[str | None] = mapped_column(String(16))
    changed_platform: Mapped[str | None] = mapped_column(String(16))
    is_handover: Mapped[int] = mapped_column(Integer, default=0)
    supposed_departure_hour: Mapped[int | None] = mapped_column(Integer)

    # 1 if the trip's service_date (its origin date, see day_type_of's
    # docstring) is a Sunday or a non-partial public holiday in this stop's
    # own station's state; 0 if neither; NULL if the station's state is
    # unresolved AND the date is only a state-specific (not all-16) holiday,
    # so it genuinely can't be decided. Filled by
    # `derive/stop_averages.py`'s label step, not at ingest time -- see
    # holiday-calendar spec.
    is_holiday: Mapped[int | None] = mapped_column(Integer)

    trip: Mapped["Trip"] = relationship(back_populates="stops")
    station: Mapped["Station"] = relationship()

    __table_args__ = (
        UniqueConstraint("trip_id", "stop_sequence", name="uq_tripstop_trip_seq"),
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
        Index("ix_ts_station_stop", "station_stop_id"),
    )

    def __repr__(self) -> str:
        return f"<TripStop trip={self.trip_id} seq={self.stop_sequence} dep_delay={self.dep_delay_s}>"


# ---------------------------------------------------------------------------
# Provenance and lookup tables
# ---------------------------------------------------------------------------


class FeedVersion(Base):
    """One downloaded static GTFS archive."""

    __tablename__ = "feed_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # fv | rv
    source_url: Mapped[str] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    downloaded_at: Mapped[int] = mapped_column(Integer)
    imported_at: Mapped[int | None] = mapped_column(Integer)
    feed_start_date: Mapped[str | None] = mapped_column(String(10))
    feed_end_date: Mapped[str | None] = mapped_column(String(10))
    n_trips: Mapped[int | None] = mapped_column(Integer)
    n_stops: Mapped[int | None] = mapped_column(Integer)
    n_station_stops: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="importing")
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_feed_version_kind_status", "kind", "status"),)

    def __repr__(self) -> str:
        return f"<FeedVersion {self.id} {self.kind} {self.status} {self.content_sha256[:8]}>"


class Route(Base):
    """A line/service: 'ICE', 'RE 1'. Scoped to a feed version."""

    __tablename__ = "route"

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_version_id: Mapped[int] = mapped_column(ForeignKey("feed_version.id"), index=True)
    gtfs_route_id: Mapped[str] = mapped_column(String(64))
    agency_id: Mapped[str | None] = mapped_column(String(64))
    short_name: Mapped[str | None] = mapped_column(String(64))
    long_name: Mapped[str | None] = mapped_column(Text)
    route_type: Mapped[int] = mapped_column(Integer)

    trains: Mapped[list["Train"]] = relationship(back_populates="route")

    __table_args__ = (
        UniqueConstraint("feed_version_id", "gtfs_route_id", name="uq_route_feed_gtfsid"),
    )

    def __repr__(self) -> str:
        return f"<Route {self.short_name!r} type={self.route_type}>"


class PublicHoliday(Base):
    """One date a state's timetable treats as a Sunday.

    Keyed by (date, state) rather than date alone: German holidays are set per
    Bundesland, so the same date can be a holiday in one state and an ordinary
    weekday in another (holiday-calendar spec). `partial` marks a city/region-
    only observance within the state (only Augsburg's Friedensfest today) --
    such a row is stored for the record but must never flag the whole state as
    a holiday; see `holidays_de.load_holiday_dates`, which excludes
    partial rows.
    """

    __tablename__ = "public_holiday"

    date: Mapped[str] = mapped_column(String(10), primary_key=True)  # 'YYYY-MM-DD'
    state: Mapped[str] = mapped_column(String(8), primary_key=True)  # 'DE-NW', ...
    name: Mapped[str | None] = mapped_column(String(64))
    partial: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        return f"<PublicHoliday {self.date} {self.state} {self.name!r}>"


class StopDelayAverage(Base):
    """Precomputed mean arrival/departure delay for one station/line/direction/
    trip-start-time/scheduled-time/holiday-flag combination.

    Full-rebuild table (`derive/stop_averages.py`), not incrementally
    maintained -- see stop-delay-averages spec's "precomputed, not live"
    requirement. `sched_time` is the station-local 'HH:MM' of the stop itself
    (departure, or arrival for a terminating call), not the trip's origin
    time -- `trip.supposed_departure_utc` moves when a backfill hour is
    missing (`[[dbrel-aggregates]]` memory), but the scheduled time at the
    station being averaged is stable.

    `trip_start_time` IS part of the key (moved in from being informational-
    only): the same line/direction/station/sched_time combination can be
    reached by two genuinely different daily services -- e.g. RE7's coupled
    portions, two trains departing their own, different origins at the same
    minute and splitting/joining en route -- and pooling them would average
    together two services a passenger would never consider "the same train".
    It is derived per TRIP, not per train (`train.first_departure_sec` is a
    per-pattern static field and can't tell two different daily start times
    of the same numbered service apart), from the IRIS run id embedded in
    `trip.source_run_key` -- see `derive/stop_averages.py`'s docstring.
    """

    __tablename__ = "stop_delay_average"

    station_id: Mapped[int] = mapped_column(ForeignKey("station.id"), primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("service_line.id"), primary_key=True)
    direction: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_start_time: Mapped[str] = mapped_column(String(5), primary_key=True)  # 'HH:MM'
    sched_time: Mapped[str] = mapped_column(String(5), primary_key=True)  # 'HH:MM'
    is_holiday: Mapped[int] = mapped_column(Integer, primary_key=True)  # 0/1

    n_arr: Mapped[int] = mapped_column(Integer, default=0)
    mean_arr_delay_s: Mapped[float | None] = mapped_column(Float)
    n_dep: Mapped[int] = mapped_column(Integer, default=0)
    mean_dep_delay_s: Mapped[float | None] = mapped_column(Float)
    built_at: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_sda_station_line", "station_id", "line_id", "direction"),
    )

    def __repr__(self) -> str:
        return (
            f"<StopDelayAverage station={self.station_id} line={self.line_id} "
            f"dir={self.direction} {self.sched_time} holiday={self.is_holiday}>"
        )


# ---------------------------------------------------------------------------
# DB Timetables (IRIS-TTS) specific tables
# ---------------------------------------------------------------------------


class TtPlanFetch(Base):
    """One /plan station-hour bucket. Idempotency for the rolling importer."""

    __tablename__ = "tt_plan_fetch"

    id: Mapped[int] = mapped_column(primary_key=True)
    eva: Mapped[int] = mapped_column(Integer)
    plan_date: Mapped[str] = mapped_column(String(10))
    plan_hour: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[int] = mapped_column(Integer)
    http_status: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    n_stops: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("eva", "plan_date", "plan_hour", name="uq_planfetch_eva_date_hour"),
    )


class TtPoll(Base):
    """One /fchg fetch of one station. The per-station listening record."""

    __tablename__ = "tt_poll"

    id: Mapped[int] = mapped_column(primary_key=True)
    sweep_id: Mapped[int] = mapped_column(Integer, index=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("station.id"))
    eva: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[int] = mapped_column(Integer)
    http_status: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    n_stops: Mapped[int] = mapped_column(Integer, default=0)
    n_matched: Mapped[int] = mapped_column(Integer, default=0)
    n_unmatched: Mapped[int] = mapped_column(Integer, default=0)
    n_changed: Mapped[int] = mapped_column(Integer, default=0)
    fetch_ms: Mapped[int | None] = mapped_column(Integer)
    parse_ms: Mapped[int | None] = mapped_column(Integer)
    store_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_ttpoll_station_time", "station_id", "fetched_at"),)

    def __repr__(self) -> str:
        return f"<TtPoll {self.id} eva={self.eva} {self.http_status}>"


class TtUnmatched(Base):
    """A /fchg change for a run we have no trip row for."""

    __tablename__ = "tt_unmatched"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_date: Mapped[str] = mapped_column(String(10))
    daily_trip_id: Mapped[str] = mapped_column(String(32))
    stop_index: Mapped[int] = mapped_column(Integer)
    eva: Mapped[int] = mapped_column(Integer)
    category: Mapped[str | None] = mapped_column(String(8))
    number: Mapped[str | None] = mapped_column(String(16))
    first_poll_id: Mapped[int | None] = mapped_column(Integer)
    last_poll_id: Mapped[int | None] = mapped_column(Integer)
    n_polls: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        UniqueConstraint(
            "service_date", "daily_trip_id", "stop_index", name="uq_ttunmatched_run_stop"
        ),
    )


class TtMessage(Base):
    """A delay cause or disruption note attached to a stop."""

    __tablename__ = "tt_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trip.id"))
    stop_sequence: Mapped[int] = mapped_column(Integer)
    msg_id: Mapped[str | None] = mapped_column(String(32))
    msg_type: Mapped[str | None] = mapped_column(String(2))
    code: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[int | None] = mapped_column(Integer)
    valid_from: Mapped[int | None] = mapped_column(Integer)
    valid_to: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int | None] = mapped_column(Integer)
    deleted: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("trip_id", "stop_sequence", "msg_id", "ts", name="uq_ttmsg"),
        Index("ix_ttmsg_code", "code"),
    )
