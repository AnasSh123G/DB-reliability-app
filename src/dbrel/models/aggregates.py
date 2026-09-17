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

# Which kind of run a row was computed from. Part of the unique key, not a pair of
# counters on a shared row: an average that mixes measured and simulated samples is
# a number nobody can interpret, and keeping them apart means the figures themselves
# are never contaminated — the UI cannot forget to check a flag it does not need.
BASIS_REAL = "REAL"
BASIS_SYNTH = "SYNTH"


class TrainStationDelay(Base):
    """How late one train usually is at one station, on one day of the week.

    Grain is (station, train, basis, dow). Not keyed on service_line: train.line_id
    is functionally determined by train_id, so adding it produces no extra rows, and
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
    basis: Mapped[str] = mapped_column(String(8))

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
        UniqueConstraint(
            "station_id", "train_id", "basis", "dow", name="uq_tsd_station_train_basis_dow"
        ),
        Index("ix_tsd_train_basis", "train_id", "basis", "dow"),
        # "Which trains are worst at this station, on this day" — served index-only.
        Index("ix_tsd_station_basis_dep", "station_id", "basis", "dow", "avg_dep_delay_s"),
    )

    def __repr__(self) -> str:
        return (
            f"<TrainStationDelay station={self.station_id} train={self.train_id} "
            f"{self.basis} dow={self.dow} n_dep={self.n_dep}>"
        )
