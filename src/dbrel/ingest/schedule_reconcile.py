"""Refresh derived delay fields when append-only schedule evidence changes."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dbrel.models import Trip, TripStop, TripScheduleSnapshot
from dbrel.ingest.timeutil import local_hour


def reconcile_facts(session: Session, trip: Trip, sequences: set[int], *,
                    as_of: int | None = None, resolved: dict | None = None) -> None:
    """Keep observations intact; invalidate delays lacking a unique reference."""
    from dbrel.ingest.dated_schedule import load_patterns
    from dbrel.ingest.finalize_tt import _rollup, _rollup_source

    facts = list(session.scalars(select(TripStop).where(
        TripStop.trip_id == trip.id, TripStop.stop_sequence.in_(sequences),
    )))
    if not facts:
        return
    planned = resolved if resolved is not None else load_patterns(session, [trip.train_id], {
        trip.train_id: {trip.service_date: trip.path_sha1},
    }, as_of=as_of).get((trip.train_id, trip.service_date), {})
    for fact in facts:
        reference = planned.get(fact.stop_sequence)
        if reference is not None and reference.station_id != fact.station_id:
            reference = None
        arrival = reference.supposed_arr if reference else None
        departure = reference.supposed_dep if reference else None
        fact.supposed_arrival_utc = arrival
        fact.supposed_departure_utc = departure
        fact.supposed_departure_hour = local_hour(departure) if departure is not None else None
        fact.station_stop_id = reference.station_stop_id if reference else None
        fact.planned_platform = reference.platform_code if reference else None
        fact.arr_delay_s = (
            fact.actual_arrival_utc - arrival
            if arrival is not None and fact.actual_arrival_utc is not None and not fact.cancelled
            else None
        )
        fact.dep_delay_s = (
            fact.actual_departure_utc - departure
            if departure is not None and fact.actual_departure_utc is not None and not fact.cancelled
            else None
        )
        fact.rule_version = 3
    session.flush()
    rows = _rollup_source(session, [trip.id]).get(trip.id, [])
    if rows:
        rollup = _rollup(rows)
        trip.final_delay_s = rollup["final_delay_s"]
        trip.max_delay_s = rollup["max_delay_s"]
    session.flush()


def refresh_run_bounds(session: Session, trip: Trip) -> None:
    """Keep partial imports from shrinking a known run's tracked extent.

    Endpoint fields bound the closure window; stop delays still require unique
    dated times. On an ambiguous destination, close after its latest possibility.
    """
    row = TripScheduleSnapshot
    groups = session.execute(select(
        row.stop_sequence, func.min(row.supposed_departure_utc),
        func.max(row.supposed_arrival_utc), func.max(row.supposed_departure_utc),
    ).where(row.trip_id == trip.id, row.path_sha1 == trip.path_sha1)
      .group_by(row.stop_sequence).order_by(row.stop_sequence)).all()
    if not groups:
        return
    first, last = groups[0], groups[-1]
    trip.supposed_departure_utc = first[1]
    trip.supposed_arrival_utc = last[2] if last[2] is not None else last[3]
    trip.n_stops_tracked = len(groups)
    session.flush()
