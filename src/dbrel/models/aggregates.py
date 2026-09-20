"""Derived rollups.

Separate from core.py on purpose. core.py holds the domain — the schedule and the
fact table — and none of it can be recreated once lost. Everything here is a
materialised summary that can be dropped and rebuilt from trip_stop at any time,
which is a different lifecycle, and the schema is careful about that distinction
elsewhere (meta.py = provenance, timetables.py = source-specific).
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from dbrel.db import Base

# How a service date is classified for TrainStopAverage. Two buckets, not seven:
# a public holiday runs a Sunday timetable, so it belongs with the Sundays, and
# everything else pools together. Saturday counts as a weekday — it runs a thinner
# timetable but it is a working day. dbrel/holidays_de.py decides which is which.
DAY_TYPE_WEEKDAY = "WEEKDAY"
DAY_TYPE_SUNDAY_HOLIDAY = "SUNDAY_HOLIDAY"


class TrainStationDelay(Base):
    """How late one train usually is at one station, on one day of the week.

    Grain is (station, train, dow). Not keyed on service_line: train.line_id is
    functionally determined by train_id, so adding it produces no extra rows, and
    upsert_line only mints a line when both category and line are present — no
    long-distance stop carries dp@l (findings §11), so every ICE and IC has
    line_id NULL and a line-keyed table would silently drop them. line_id is kept
    as an attribute so the board can group without a second join.

    Day of week is part of the key rather than averaged over, because a weekday and
    a Sunday are different railways: different stopping patterns, different traffic,
    different delays. One number spanning both describes neither.
    """

    __tablename__ = "train_station_delay"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("station.id", ondelete="CASCADE"))
    train_id: Mapped[int] = mapped_column(ForeignKey("train.id", ondelete="CASCADE"))

    # 0=Monday..6=Sunday, i.e. Python's date.weekday(), taken from trip.service_date
    # — the planned departure date at the run's ORIGIN, so a run that crosses
    # midnight keeps the weekday it set out on instead of splitting in two.
    #
    # Note this is not SQLite's strftime('%w'), which is 0=Sunday. The day views in
    # views.py have to convert; a test pins the two against each other.
    dow: Mapped[int] = mapped_column(Integer)

    line_id: Mapped[int | None] = mapped_column(ForeignKey("service_line.id"))
    segment: Mapped[str | None] = mapped_column(String(4))

    # Counted separately: a stop can have a departure and no arrival (origin), or
    # an arrival and no departure (terminus), so one sample count would be a lie
    # for the half that does not exist.
    n_arr: Mapped[int] = mapped_column(Integer, default=0)
    n_dep: Mapped[int] = mapped_column(Integer, default=0)
    n_runs: Mapped[int] = mapped_column(Integer, default=0)
    n_cancelled: Mapped[int] = mapped_column(Integer, default=0)
    # Trusted + cancelled. The denominator for a cancellation rate; NO_DATA and
    # PREDICTION_ONLY are excluded from it because they are unknowns, not events.
    n_known: Mapped[int] = mapped_column(Integer, default=0)

    avg_arr_delay_s: Mapped[float | None] = mapped_column(Float)
    avg_dep_delay_s: Mapped[float | None] = mapped_column(Float)
    p50_arr_delay_s: Mapped[int | None] = mapped_column(Integer)
    p50_dep_delay_s: Mapped[int | None] = mapped_column(Integer)
    p90_arr_delay_s: Mapped[int | None] = mapped_column(Integer)
    p90_dep_delay_s: Mapped[int | None] = mapped_column(Integer)
    max_dep_delay_s: Mapped[int | None] = mapped_column(Integer)

    # Share under config.PUNCTUAL_S, as a rate in 0..1.
    pct_arr_punctual: Mapped[float | None] = mapped_column(Float)
    pct_dep_punctual: Mapped[float | None] = mapped_column(Float)

    first_service_date: Mapped[str | None] = mapped_column(String(10))
    last_service_date: Mapped[str | None] = mapped_column(String(10))
    computed_at: Mapped[int] = mapped_column(Integer)
    rule_version: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("station_id", "train_id", "dow", name="uq_tsd_station_train_dow"),
        Index("ix_tsd_train_dow", "train_id", "dow"),
        # "Which trains are worst at this station, on this day" — served index-only.
        Index("ix_tsd_station_dow_dep", "station_id", "dow", "avg_dep_delay_s"),
    )

    def __repr__(self) -> str:
        return (
            f"<TrainStationDelay station={self.station_id} train={self.train_id} "
            f"dow={self.dow} n_dep={self.n_dep}>"
        )


class TrainStopAverage(Base):
    """How late one train usually is at one stop, pooled by day type.

    Grain is (day_type, train, station, sched_sec): one row per *call* a train
    makes, per kind of day. Read on its own it reconstructs a journey — the train,
    its line, every station it calls at, when it is meant to be there, and how late
    it actually is — which train_station_delay cannot do, because that table splits
    by weekday and carries no scheduled time.

    Two things it deliberately does differently:

    * **Monday to Saturday pool into one figure.** Splitting by weekday divides the
      samples by seven, so a month of collection leaves four runs per row. Pooling
      the working days is what makes the number mean "usually" after weeks rather
      than months. Sundays and public holidays are the one split that survives,
      because they are a different timetable rather than the same one on a
      different day.
    * **The scheduled time is part of the key.** A train number is not one service:
      the same number can run 06:34 Monday to Friday and 07:15 on Saturday, and
      averaging those together describes neither. Keying on the time keeps them
      apart without needing to know in advance which numbers do it.

    Metric column names are identical to TrainStationDelay on purpose — the web
    layer's _average() helper reads them by getattr, so these rows work with it
    unchanged.
    """

    __tablename__ = "train_stop_average"

    id: Mapped[int] = mapped_column(primary_key=True)
    day_type: Mapped[str] = mapped_column(String(16))
    train_id: Mapped[int] = mapped_column(ForeignKey("train.id", ondelete="CASCADE"))
    station_id: Mapped[int] = mapped_column(ForeignKey("station.id", ondelete="CASCADE"))

    # Seconds from the service-day anchor ("noon minus twelve hours"), NOT a
    # wall-clock second-of-day: the noon anchor is what keeps the value stable
    # across the two DST days each year, and a stop after midnight legitimately
    # exceeds 86400. See gtfs/timeutil.py.
    #
    # The departure time when there is one, the arrival when there is not. Taking
    # only the departure would make this NULL at every terminus, and SQLite counts
    # NULLs as distinct in a unique constraint — every terminus row would insert
    # afresh on each run instead of upserting.
    sched_sec: Mapped[int] = mapped_column(Integer)
    # Which of the two sched_sec came from, so a reader never has to guess.
    sched_kind: Mapped[str] = mapped_column(String(3))
    sched_arr_sec: Mapped[int | None] = mapped_column(Integer)
    sched_dep_sec: Mapped[int | None] = mapped_column(Integer)

    # The line — RE1, S1, RB40. This is what a passenger calls the "route";
    # train.route_id is GTFS-only and is NULL for every Timetables train.
    line_id: Mapped[int | None] = mapped_column(ForeignKey("service_line.id"))
    segment: Mapped[str | None] = mapped_column(String(4))

    # Position along the journey, for ordering a route readably. An attribute and
    # not part of the key: it is the sparse IRIS stopIndex, relative to the path as
    # stated on the day, so a handful of (train, station) pairs carry two different
    # values and keying on it would split their samples for no reason.
    stop_sequence: Mapped[int | None] = mapped_column(Integer)

    n_arr: Mapped[int] = mapped_column(Integer, default=0)
    n_dep: Mapped[int] = mapped_column(Integer, default=0)
    n_runs: Mapped[int] = mapped_column(Integer, default=0)
    n_cancelled: Mapped[int] = mapped_column(Integer, default=0)
    n_known: Mapped[int] = mapped_column(Integer, default=0)

    avg_arr_delay_s: Mapped[float | None] = mapped_column(Float)
    avg_dep_delay_s: Mapped[float | None] = mapped_column(Float)
    p50_arr_delay_s: Mapped[int | None] = mapped_column(Integer)
    p50_dep_delay_s: Mapped[int | None] = mapped_column(Integer)
    p90_arr_delay_s: Mapped[int | None] = mapped_column(Integer)
    p90_dep_delay_s: Mapped[int | None] = mapped_column(Integer)
    max_dep_delay_s: Mapped[int | None] = mapped_column(Integer)

    pct_arr_punctual: Mapped[float | None] = mapped_column(Float)
    pct_dep_punctual: Mapped[float | None] = mapped_column(Float)

    first_service_date: Mapped[str | None] = mapped_column(String(10))
    last_service_date: Mapped[str | None] = mapped_column(String(10))
    computed_at: Mapped[int] = mapped_column(Integer)
    rule_version: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint(
            "day_type",
            "train_id",
            "station_id",
            "sched_sec",
            name="uq_tsa_daytype_train_station_time",
        ),
        # "Show me this train's whole journey in order" — the common read.
        Index("ix_tsa_train", "train_id", "day_type", "sched_sec"),
        # "Which trains are worst at this station" — served index-only.
        Index("ix_tsa_station", "station_id", "day_type", "avg_dep_delay_s"),
    )

    def __repr__(self) -> str:
        return (
            f"<TrainStopAverage train={self.train_id} station={self.station_id} "
            f"{self.day_type} t={self.sched_sec} n_dep={self.n_dep}>"
        )
