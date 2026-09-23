"""Resolve every station's Bundesland from its coordinates.

Python loop over stations (a few thousand rows), not a SQL operation --
point-in-polygon has no SQL representation here, and the constraint on
avoiding row-by-row work is about `trip_stop` (tens of millions of rows), not
`station`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.geo_states import state_for
from dbrel.models import Station


@dataclass
class ResolveReport:
    per_state: dict[str, int] = field(default_factory=dict)
    unresolved: int = 0
    unresolved_names: list[str] = field(default_factory=list)
    total: int = 0


def resolve_station_states(session: Session, *, name_sample: int = 20) -> ResolveReport:
    """Set `Station.state` for every station with lat/lon. Re-runnable.

    Overwrites any previously resolved state (the polygon data and the
    resolution logic are deterministic, so re-running is idempotent -- it just
    recomputes the same answer, unlike the additive holiday load).
    """
    report = ResolveReport()
    stations = session.execute(select(Station)).scalars().all()
    for station in stations:
        report.total += 1
        state = state_for(station.lat, station.lon) if station.lat is not None and station.lon is not None else None
        station.state = state
        if state is None:
            report.unresolved += 1
            if len(report.unresolved_names) < name_sample:
                report.unresolved_names.append(station.name)
        else:
            report.per_state[state] = report.per_state.get(state, 0) + 1
    return report
