"""Provenance and lookup tables: which import produced which rows."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dbrel.db import Base


class FeedVersion(Base):
    """One downloaded static GTFS archive.

    gtfs.de regenerates the feeds roughly weekly and reuses the literal string
    'latest-free' as feed_version, so the content hash is the only reliable identity.
    Re-importing identical bytes is a no-op because content_sha256 is unique.
    """

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
    # importing | active | superseded | failed
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

    trains: Mapped[list["Train"]] = relationship(back_populates="route")  # noqa: F821

    __table_args__ = (
        UniqueConstraint("feed_version_id", "gtfs_route_id", name="uq_route_feed_gtfsid"),
    )

    def __repr__(self) -> str:
        return f"<Route {self.short_name!r} type={self.route_type}>"
