"""Tables specific to the DB Timetables (IRIS-TTS) source.

Four tables, each answering a question nothing else can:

* `tt_plan_fetch` — which /plan station-hour buckets are already captured, so the
  roller skips them instead of re-spending the rate limit. /plan is pre-generated
  and never changes, so a 200 is final.
* `tt_poll` — one /fchg fetch of one station, failures included. This is the
  listening record, and it is what makes NO_DATA honest: a fetch is evidence about
  one station only, so a missing value can only be read as "the railway said
  nothing" if we were actually reading *that* station at the time.
* `tt_unmatched` — a /fchg change for a run we hold no schedule for. A rising count
  is the alarm that the /plan roller has fallen behind, and because /plan serves
  only today and tomorrow, that planned data is being lost permanently.
* `tt_message` — delay causes. Data the GTFS pipeline never had at all.

Recorded times themselves do not live here. The poller writes them straight into
`trip_stop`, because an archival reader only ever ingests values that have already
stopped moving: there is no revision history to keep, and a separate observation
log was a 1.05-row-per-fact copy of the same data.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from dbrel.db import Base


class TtPlanFetch(Base):
    """One /plan station-hour bucket. Idempotency for the rolling importer.

    /plan is pre-generated and never changes, so a bucket already fetched with 200
    is skipped; the content hash lets that claim be audited later.
    """

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
    """One /fchg fetch of one station. The per-station listening record.

    This is the table that makes the OBSERVED grade honest: a stop may only be
    graded on the strength of a poll of *its own* station.
    """

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
    """A /fchg change for a run we have no trip row for.

    A growing count is the alarm that the /plan roller has fallen behind — and,
    since /plan only serves today and tomorrow, that the planned data is being
    lost permanently.
    """

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
    """A delay cause or disruption note attached to a stop.

    Data the GTFS pipeline never had at all, and the most obviously useful label
    for the eventual training dataset.
    """

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
