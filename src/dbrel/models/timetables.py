"""Tables specific to the DB Timetables (IRIS-TTS) source.

Kept separate from the rt_* tables rather than overloading them, for reasons that
outlive the migration:

* `rt_poll` has no station dimension, and it *is* the listening record — its whole
  purpose is answering "were we watching at time T". Under Timetables one fetch
  covers one station, so that question is meaningless without "...at station S".
* `rt_observation.schedule_relationship` is a GTFS int enum. IRIS carries ps/cs in
  {p,a,c} *per event* plus a separate cancellation timestamp, and squeezing that
  into an int would lose the arrival-cancelled-but-departure-not case.
* Both sources run simultaneously and permanently, so this is not a transition.

Column names deliberately match rt_observation wherever the meaning matches, so
derive/finalize.py barely notices which instrument produced a row.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from dbrel.db import Base


class TrackedStation(Base):
    """A station we actually poll. The operational lifecycle of the station set.

    Separate from `station` because gtfs/static_import.py bulk-inserts Station rows
    and would clobber operational columns, and because tracking has a lifecycle
    (priority, high-water marks, error counters) that a shared dimension table
    should not carry. The eva itself lives on `station` — it is a property of the
    place, and untracked stations need it to resolve ppth.
    """

    __tablename__ = "tracked_station"

    station_id: Mapped[int] = mapped_column(ForeignKey("station.id"), primary_key=True)
    eva: Mapped[int] = mapped_column(Integer, index=True)

    # 1 = every sweep, 2 = every other, 3 = every third. Lets a large set keep
    # high resolution where it matters without exceeding 60 rpm.
    poll_priority: Mapped[int] = mapped_column(Integer, default=1)

    added_at: Mapped[int | None] = mapped_column(Integer)
    removed_at: Mapped[int | None] = mapped_column(Integer)

    # Share of ppth neighbours that matched the GTFS pattern at verification time.
    verify_overlap: Mapped[float | None] = mapped_column(Float)
    verified_at: Mapped[int | None] = mapped_column(Integer)

    # 'YYYY-MM-DD:HH' high-water mark for the rolling /plan importer.
    last_plan_hour: Mapped[str | None] = mapped_column(String(13))
    last_fchg_at: Mapped[int | None] = mapped_column(Integer)
    # Measured median seconds between polls. Diagnostic only: it used to scale a
    # per-station settle window, which the archival reader no longer needs — a
    # value read after passage is already final, so there is nothing to wait out.
    median_poll_gap_s: Mapped[int | None] = mapped_column(Integer)

    n_fchg_polls: Mapped[int] = mapped_column(Integer, default=0)
    n_fchg_errors: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<TrackedStation station={self.station_id} eva={self.eva}>"


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


class TtObservation(Base):
    """One published value for one stop of one run, recorded only when it changed.

    /fchg returns every known change over a many-hour horizon on every poll, so
    storing unchanged repeats would grow the table by the whole horizon each sweep.

    Dedup is enforced by the database on value_sha rather than by comparing tuples
    in Python. The tuple approach shipped three different column orders — the row
    written, the row read back, and the value compared — and leaked 34% duplicates,
    which also corrupted observed_at into "when we last rewrote an unchanged value",
    the exact quantity the grading rule reads as "when it was last revised".
    """

    __tablename__ = "tt_observation"

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("tt_poll.id"))
    trip_id: Mapped[int] = mapped_column(ForeignKey("trip.id"))
    stop_sequence: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[int] = mapped_column(Integer)

    # Digest of the published value below. A plain UniqueConstraint over those
    # columns would not dedupe anything: SQLite treats NULLs as distinct, and most
    # of them are legitimately NULL (an origin stop has no arrival).
    value_sha: Mapped[str] = mapped_column(String(40))

    # Same names as rt_observation: finalize prefers *_time_utc and computes
    # nothing differently for a Timetables row.
    arr_delay_s: Mapped[int | None] = mapped_column(Integer)
    dep_delay_s: Mapped[int | None] = mapped_column(Integer)
    arr_time_utc: Mapped[int | None] = mapped_column(Integer)
    dep_time_utc: Mapped[int | None] = mapped_column(Integer)

    # IRIS specifics that have no GTFS-RT equivalent.
    arr_status: Mapped[str | None] = mapped_column(String(1))  # p | a | c
    dep_status: Mapped[str | None] = mapped_column(String(1))
    # cp only, never coalesced with pp. The planned platform belongs to the
    # schedule (station_stop.platform_code); folding it in here made "changed"
    # and "planned" indistinguishable downstream.
    arr_platform: Mapped[str | None] = mapped_column(String(16))
    dep_platform: Mapped[str | None] = mapped_column(String(16))
    # clt. One column: a one-sided cancellation still publishes one timestamp, and
    # which side it applies to is recoverable from arr_status / dep_status.
    cancelled_at_utc: Mapped[int | None] = mapped_column(Integer)
    changed_path: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("trip_id", "stop_sequence", "value_sha", name="uq_ttobs_trip_seq_value"),
        Index("ix_ttobs_trip_seq_time", "trip_id", "stop_sequence", "observed_at"),
    )


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


class TrainConflict(Base):
    """A run that resolved to an existing train but disagreed about it.

    Written instead of silently attaching or silently dropping: the run is parked
    on a variant identity and the disagreement becomes queryable. Same principle
    as rt_unmatched — make corruption visible rather than plausible.
    """

    __tablename__ = "train_conflict"

    id: Mapped[int] = mapped_column(primary_key=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("train.id"), index=True)
    service_date: Mapped[str] = mapped_column(String(10))
    field: Mapped[str] = mapped_column(String(24))  # operator | path_sha1 | endpoints
    expected: Mapped[str | None] = mapped_column(Text)
    observed: Mapped[str | None] = mapped_column(Text)
    run_key: Mapped[str] = mapped_column(String(64))
    first_seen: Mapped[int] = mapped_column(Integer)


class TrainMerge(Base):
    """Audit trail for `dbrel merge-trains`. Merges are reversible by design."""

    __tablename__ = "train_merge"

    id: Mapped[int] = mapped_column(primary_key=True)
    kept_id: Mapped[int] = mapped_column(ForeignKey("train.id"))
    merged_id: Mapped[int] = mapped_column(ForeignKey("train.id"))
    merged_identity_key: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)
    merged_at: Mapped[int] = mapped_column(Integer)


class TripLink(Base):
    """A relationship between two runs: a number change, or a wing.

    A run that changes number is two trains and two trips, linked — the number is
    the identity, and the timetable genuinely treats them as two trains. Wings
    each keep their own number; their delays are correlated, not independent,
    which any aggregate over them has to account for.
    """

    __tablename__ = "trip_link"

    id: Mapped[int] = mapped_column(primary_key=True)
    a_trip_id: Mapped[int] = mapped_column(ForeignKey("trip.id", ondelete="CASCADE"))
    b_trip_id: Mapped[int | None] = mapped_column(ForeignKey("trip.id", ondelete="CASCADE"))
    relation: Mapped[str] = mapped_column(String(16))  # NUMBER_CHANGE | WING | TRANSITION
    at_stop_index: Mapped[int | None] = mapped_column(Integer)
    # Kept even when resolution succeeds: <ref> carries only category+number and
    # can be ambiguous, so the raw value is the only auditable record.
    raw_ref: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("a_trip_id", "b_trip_id", "relation", name="uq_triplink"),
        Index("ix_triplink_b", "b_trip_id"),
    )


class TrainXref(Base):
    """Reporting-only crosswalk between a GTFS train and a Timetables train.

    Never a merge and never joined into a user-facing series without a flag. Its
    real value is cross-source validation: do gtfs.de and IRIS agree about the
    same run's delay at the same stop?
    """

    __tablename__ = "train_xref"

    gtfs_train_id: Mapped[int] = mapped_column(ForeignKey("train.id"), primary_key=True)
    tt_train_id: Mapped[int] = mapped_column(ForeignKey("train.id"), primary_key=True)
    method: Mapped[str] = mapped_column(String(24))  # ENDPOINTS_TIME | MANUAL
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[str | None] = mapped_column(Text)
