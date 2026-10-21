"""A changed run key must refresh every cached reference to its physical trip."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dbrel.config import TZ
from dbrel.db import Base
from dbrel.ingest.changes_poller import run_tt_poll
from dbrel.ingest.client import Response
from dbrel.ingest.plan_import import import_plan
from dbrel.models import QUALITY_RECORDED, Station, StationStop, Trip, TripScheduleSnapshot, TripStop

EVA = 8001506
R1 = "111-2608280320"
R2 = "222-2608280320"
NOW = int(datetime(2026, 8, 28, 5, tzinfo=TZ).timestamp())


def _full(run, *, onward, sequence=4):
    return (
        f'<s id="{run}-{sequence}" eva="{EVA}">'
        '<tl t="p" o="NXRE" c="NX" n="26801"/>'
        '<ar pt="2608280359" ct="2608280401" pp="1" l="RE1" ppth="Köln Hbf"/>'
        f'<dp pt="2608280400" ct="2608280402" pp="1" l="RE1" ppth="{onward}"/>'
        '</s>'
    )


def _xml(stops):
    return f'<timetable station="Dormagen" eva="{EVA}">{stops}</timetable>'.encode()


class _Source:
    def __init__(self, changes):
        self.body = _xml(changes)

    def plan(self, eva, date, hour):
        return Response(status=200, body=_xml(_full(R1, onward="Neuss Hbf")), elapsed_ms=0)

    def changes(self, eva):
        return Response(status=200, body=self.body, elapsed_ms=0)


@pytest.mark.parametrize("late_r1", [True, False])
def test_same_trip_new_run_key_refreshes_schedule_and_realized_refs(late_r1):
    """R2 repoints R1's trip; later receipts and earlier facts must use that path."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    # The early delta is already queued for realization when R2 changes the path.
    early = f'<s id="{R1}-4" eva="{EVA}"><ar ct="2608280401"/><dp ct="2608280402"/></s>'
    changes = early + _full(R2, onward="Düsseldorf Hbf")
    if late_r1:
        changes += _full(R1, onward="Neuss Hbf").replace('pp="1"', 'pp="2"')
    source = _Source(changes)
    with Session(engine) as session:
        session.add(Station(natural_key="dormagen", name="Dormagen", lat=51.099,
                            lon=6.837, eva=EVA, tracked=1))
        session.flush()
        import_plan(session, source, date="2026-08-28", hours=[3], force=True, now=NOW - 3600)
        trip = session.scalars(select(Trip)).one()
        original_path = trip.path_sha1
        original_id = trip.id

        result = run_tt_poll(session, source, priority_max=99, now=NOW)
        session.flush()

        trip = session.scalars(select(Trip)).one()
        assert trip.id == original_id  # R2 reached the train/date existing_id branch.
        assert trip.path_sha1 != original_path
        receipts = session.scalars(select(TripScheduleSnapshot).where(
            TripScheduleSnapshot.source_kind == "fchg")).all()
        assert receipts
        assert {receipt.path_sha1 for receipt in receipts} == {trip.path_sha1}
        if late_r1:
            assert any(receipt.source_stop_id == f"{R1}-4" for receipt in receipts)
        fact = session.scalars(select(TripStop)).one()
        assert fact.trip_id == original_id
        assert fact.quality == QUALITY_RECORDED
        assert fact.actual_departure_utc == NOW - 58 * 60
        assert fact.dep_delay_s == 120
        assert session.get(StationStop, fact.station_stop_id).path_sha1 == trip.path_sha1
        assert result.refs_refreshed > 0


def test_early_r1_delta_is_realized_after_pending_r2_changes_path():
    """A future R2 observation must not hide the need to refresh R1's ref."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    early_r1 = (
        f'<s id="{R1}-4" eva="{EVA}">'
        '<ar ct="2608280401"/><dp ct="2608280402"/></s>'
    )
    future_r2 = _full(R2, onward="Düsseldorf Hbf")
    future_r2 = future_r2.replace('ct="2608280401"', 'ct="2608280601"')
    future_r2 = future_r2.replace('ct="2608280402"', 'ct="2608280602"')
    source = _Source(early_r1 + future_r2)

    with Session(engine) as session:
        session.add(Station(natural_key="dormagen", name="Dormagen", lat=51.099,
                            lon=6.837, eva=EVA, tracked=1))
        session.flush()
        import_plan(session, source, date="2026-08-28", hours=[3], force=True, now=NOW - 3600)
        trip = session.scalars(select(Trip)).one()
        original_path = trip.path_sha1

        result = run_tt_poll(session, source, priority_max=99, now=NOW)
        session.flush()

        trip = session.scalars(select(Trip)).one()
        assert trip.path_sha1 != original_path, "the full R2 row must repoint the trip path"
        assert result.pending == 1, "R2's 06:01/06:02 times are still in the future at 05:00"
        receipts = session.scalars(select(TripScheduleSnapshot).where(
            TripScheduleSnapshot.source_kind == "fchg")).all()
        assert any(receipt.source_stop_id == f"{R2}-4" for receipt in receipts)
        fact = session.scalars(select(TripStop)).one()
        assert fact.trip_id == trip.id
        assert fact.quality == QUALITY_RECORDED
        assert fact.actual_departure_utc == NOW - 58 * 60
        assert fact.dep_delay_s == 120
        assert session.get(StationStop, fact.station_stop_id).path_sha1 == trip.path_sha1
        assert result.refs_refreshed > 0
