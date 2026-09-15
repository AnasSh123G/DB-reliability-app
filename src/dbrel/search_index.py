"""Populate search_alias for stations.

search_alias has carried a station_id column, and a CHECK permitting it, since the
schema was written — but nothing ever wrote a row, so station search had no index
at all and the only way to find a station was an exact name. This fills it, on the
same prefix-scan path (ix_alias_lookup) that trains and lines already use.

Rows are deleted and rewritten rather than upserted. uq_alias_norm_target spans
(alias_norm, train_id, line_id, station_id), and for a station alias two of those
are NULL — SQLite treats NULLs in a unique index as distinct, so the constraint
can never fire and ON CONFLICT DO NOTHING would silently duplicate on every run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from dbrel.identity import station_aliases
from dbrel.models import SearchAlias, Station

_STATION_KINDS = ("STATION_NAME", "STATION_SHORT", "STATION_TOKEN", "STATION_DS100", "STATION_EVA")


@dataclass(slots=True)
class IndexResult:
    stations: int = 0
    aliases_written: int = 0
    aliases_deleted: int = 0
    elapsed_s: float = 0.0


def index_stations(
    session: Session,
    *,
    station_ids: list[int] | None = None,
    progress=lambda msg: None,
) -> IndexResult:
    started = time.monotonic()
    result = IndexResult()

    query = select(Station.id, Station.name, Station.ds100, Station.eva)
    if station_ids:
        query = query.where(Station.id.in_(station_ids))
    stations = session.execute(query).all()
    result.stations = len(stations)
    if not stations:
        progress("  no stations to index; run import-static first")
        return result

    scope = delete(SearchAlias).where(SearchAlias.kind.in_(_STATION_KINDS))
    if station_ids:
        scope = scope.where(SearchAlias.station_id.in_(station_ids))
    result.aliases_deleted = session.execute(scope).rowcount

    rows = [
        {
            "alias_norm": alias_norm,
            "alias_display": display,
            "kind": kind,
            "weight": weight,
            "station_id": station_id,
        }
        for station_id, name, ds100, eva in stations
        for alias_norm, display, kind, weight in station_aliases(name, ds100, eva)
    ]

    for chunk in range(0, len(rows), 5000):
        session.execute(insert(SearchAlias), rows[chunk : chunk + 5000])
    result.aliases_written = len(rows)

    session.flush()
    progress(f"  {result.stations:,} stations, {result.aliases_written:,} aliases")
    result.elapsed_s = time.monotonic() - started
    return result
