"""The raw realtime stream: what was published, when, and whether we were listening.

These tables are the source of truth from which actual times are derived. Keeping
them means a wrong derivation rule can be corrected and re-run; without them, a bad
rule destroys the history permanently.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dbrel.db import Base


class RtPoll(Base):
    """One fetch of the realtime feed, successful or not.

    This is the listening record. Without it there is no way to distinguish "the
    train was on time" from "nobody was watching when it passed", which is the
    difference between an OBSERVED and a PREDICTION_ONLY grade.
    """

    __tablename__ = "rt_poll"

    id: Mapped[int] = mapped_column(primary_key=True)
    fetched_at: Mapped[int] = mapped_column(Integer, index=True)
    feed_timestamp: Mapped[int | None] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(Integer)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    n_entities: Mapped[int | None] = mapped_column(Integer)
    n_trip_updates: Mapped[int | None] = mapped_column(Integer)
    n_matched: Mapped[int | None] = mapped_column(Integer)
    n_unmatched: Mapped[int | None] = mapped_column(Integer)
    n_stop_updates: Mapped[int | None] = mapped_column(Integer)
    n_changed: Mapped[int | None] = mapped_column(Integer)
    fetch_ms: Mapped[int | None] = mapped_column(Integer)
    parse_ms: Mapped[int | None] = mapped_column(Integer)
    store_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<RtPoll {self.id} at={self.fetched_at} matched={self.n_matched}>"


class RtObservation(Base):
    """A published prediction for one stop of one trip, recorded when it changed.

    Append-only, and written only when a value differs from the last one stored for
    the same (trip, stop). An unchanged prediction re-published every poll carries no
    information, so storing it would be pure growth.
    """

    __tablename__ = "rt_observation"

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("rt_poll.id"))
    trip_id: Mapped[int] = mapped_column(ForeignKey("trip.id"))
    stop_sequence: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[int] = mapped_column(Integer)

    arr_delay_s: Mapped[int | None] = mapped_column(Integer)
    dep_delay_s: Mapped[int | None] = mapped_column(Integer)
    arr_time_utc: Mapped[int | None] = mapped_column(Integer)
    dep_time_utc: Mapped[int | None] = mapped_column(Integer)
    # GTFS-RT StopTimeUpdate.ScheduleRelationship: 0 SCHEDULED, 1 SKIPPED, 2 NO_DATA
    schedule_relationship: Mapped[int | None] = mapped_column(Integer)

    poll: Mapped["RtPoll"] = relationship()

    __table_args__ = (
        Index("ix_obs_trip_seq_time", "trip_id", "stop_sequence", "observed_at"),
        Index("ix_obs_poll", "poll_id"),
    )

    def __repr__(self) -> str:
        return f"<RtObservation trip={self.trip_id} seq={self.stop_sequence} dep={self.dep_delay_s}>"


class RtUnmatched(Base):
    """A realtime trip that resolves to no known train.

    Realtime descriptors carry only trip_id and start_date, so an unresolvable one
    cannot be recovered by any fallback. Recording it here makes a broken id join
    visible as a growing table instead of as silently missing data.
    """

    __tablename__ = "rt_unmatched"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_date: Mapped[str] = mapped_column(String(10))
    gtfs_trip_id: Mapped[str] = mapped_column(String(64))
    gtfs_route_id: Mapped[str | None] = mapped_column(String(64))
    start_time: Mapped[str | None] = mapped_column(String(16))
    n_stop_updates: Mapped[int | None] = mapped_column(Integer)
    first_poll_id: Mapped[int] = mapped_column(ForeignKey("rt_poll.id"))
    last_poll_id: Mapped[int] = mapped_column(ForeignKey("rt_poll.id"))
    n_polls: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        Index("ix_unmatched_date_trip", "service_date", "gtfs_trip_id", unique=True),
    )
