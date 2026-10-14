"""Index-backed receipt inserts retain count/idempotency across batch boundaries."""
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.ingest.dated_schedule import PlanReceipt, record_plans
from dbrel.ingest.parser import parse_timetable
from dbrel.models import Station, Train, Trip, TripScheduleSnapshot


def test_receipts_crossing_insert_batches_count_only_new_identities():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    stop = parse_timetable(b'<timetable eva="8000123"><s id="123-2609011200-1">'
                           b'<tl c="RE" n="42"/><ar pt="2609011200"/>'
                           b'</s></timetable>').stops[0]
    with Session(engine) as session:
        station = Station(natural_key='s', name='Stop', lat=1, lon=1)
        train = Train(source=SOURCE_TT, identity_kind='NUMBERED', identity_key='tts:RE/42')
        session.add_all([station, train]); session.flush()
        trip = Trip(train_id=train.id, service_date='2026-09-01', source=SOURCE_TT,
                    source_run_key=stop.run_key, path_sha1='a' * 40)
        session.add(trip); session.flush()
        receipts = [(stop, PlanReceipt(i, 'b' * 64, eva=8000123)) for i in range(85)]
        mapping = {8000123: station.id}
        assert record_plans(session, trip.id, trip.path_sha1, receipts, mapping) == 85
        assert record_plans(session, trip.id, trip.path_sha1, receipts, mapping) == 0
        new = (stop, PlanReceipt(1000, 'b' * 64, eva=8000123))
        # Include duplicate identities within the same statement and across batches.
        assert record_plans(session, trip.id, trip.path_sha1,
                            receipts + [new, new], mapping) == 1
        assert session.scalar(select(func.count()).select_from(TripScheduleSnapshot)) == 86
    engine.dispose()
