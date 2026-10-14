"""A damaged newest archive payload must not erase earlier valid evidence."""
import hashlib
import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.ingest.client import Response
from dbrel.ingest.plan_import import import_plan
from dbrel.models import Station, TripScheduleSnapshot, TripStop


def test_malformed_latest_plan_preserves_valid_earlier_receipt():
    eva = 8000123
    body = (f'<timetable eva="{eva}"><s id="123-2609011200-1">'
            '<tl c="RE" n="42" o="OP"/>'
            '<ar pt="2609011200"/><dp pt="2609011200"/>'
            '</s></timetable>').encode()
    earlier = Response(200, body, 0, recorded_at=100)
    latest = Response(200, b'<broken', 0, recorded_at=200, plan_history=(earlier,))

    class Source:
        def plan(self, *_args):
            return latest

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Station(natural_key='s', name='Stop', lat=1, lon=1, eva=eva, tracked=1))
        session.flush()
        result = import_plan(session, Source(), date='2026-09-01', hours=[12], force=True,
                             workers=1, now=200)
        evidence = list(session.scalars(select(TripScheduleSnapshot)))
        assert len(evidence) == 1
        assert evidence[0].observed_at == 100
        assert evidence[0].source_content_sha256 == hashlib.sha256(body).hexdigest()
        assert result.errors, 'the damaged response must also remain visible'
    engine.dispose()


@pytest.mark.parametrize('cancellation_time', [' clt="2609011155"', ''])
def test_pure_cancellation_delta_keeps_cancelled_fact_without_inventing_times(cancellation_time):
    from dbrel.ingest.changes_poller import run_tt_poll
    from dbrel.ingest.parser import parse_ts
    eva = 8000123
    plan = (f'<timetable eva="{eva}"><s id="123-2609011200-1">'
            '<tl c="RE" n="42" o="OP"/>'
            '<ar pt="2609011200"/><dp pt="2609011200"/>'
            '</s></timetable>').encode()
    delta = (f'<timetable eva="{eva}"><s id="123-2609011200-1">'
             f'<ar cs="c"{cancellation_time}/></s></timetable>').encode()
    now = parse_ts('2609011300')

    class Source:
        def plan(self, *_args):
            return Response(200, plan, 0, recorded_at=now - 7200)

        def changes(self, *_args):
            return Response(200, delta, 0, recorded_at=now)

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Station(natural_key='s', name='Stop', lat=1, lon=1, eva=eva, tracked=1))
        session.flush()
        source = Source()
        import_plan(session, source, date='2026-09-01', hours=[12], force=True, workers=1, now=now)
        run_tt_poll(session, source, priority_max=99, now=now)
        [fact] = session.scalars(select(TripStop)).all()
        assert fact.quality == 'CANCELLED' and fact.arr_cancelled == 1
        assert fact.actual_arrival_utc is None and fact.actual_departure_utc is None
        assert fact.arr_delay_s is None and fact.dep_delay_s is None
    engine.dispose()
