"""Re-fetching a /plan bucket must update its tt_plan_fetch row, not duplicate it.

A bucket that previously 404'd (tomorrow not yet published, or past the ~14h
retention window, docs/probe-findings.md §24) is retried on every later import
that covers the same hour on purpose — which the collect orchestrator's
overlapping window does deliberately, so a slow cycle never leaves a gap. That
retry must not violate uq_planfetch_eva_date_hour.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.models import Station, TtPlanFetch
from dbrel.timetables.client import Response
from dbrel.timetables.plan_import import import_plan

EVA = 8000041


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Station(
                natural_key="bochum@51.478,7.223",
                name="Bochum Hbf",
                lat=51.478,
                lon=7.223,
                eva=EVA,
                tracked=1,
            )
        )
        session.commit()
        yield session


class _FakeClient:
    """Scripted /plan responses, one per call, in order."""

    def __init__(self, responses: list[Response]) -> None:
        self._responses = list(responses)

    def plan(self, eva: int, date: str, hour: int) -> Response:
        return self._responses.pop(0)


def _row(session: Session) -> TtPlanFetch:
    return session.execute(
        select(TtPlanFetch).where(
            TtPlanFetch.eva == EVA, TtPlanFetch.plan_date == "2026-09-16", TtPlanFetch.plan_hour == 9
        )
    ).scalar_one()


def test_retrying_a_404_bucket_updates_in_place_instead_of_duplicating(session):
    client = _FakeClient([Response(status=404, body=b"", elapsed_ms=1)])
    import_plan(session, client, date="2026-09-16", hours=[9])
    session.flush()

    first = _row(session)
    assert first.http_status == 404

    # Same (eva, date, hour), now published. Must not raise IntegrityError.
    client = _FakeClient([Response(status=200, body=b"<timetable/>", elapsed_ms=1)])
    import_plan(session, client, date="2026-09-16", hours=[9])
    session.flush()

    count = session.scalar(
        select(func.count()).select_from(TtPlanFetch).where(TtPlanFetch.eva == EVA)
    )
    assert count == 1, "the retry must update the existing row, not insert a second one"
    assert _row(session).http_status == 200


def test_a_200_bucket_is_never_refetched(session):
    client = _FakeClient([Response(status=200, body=b"<timetable/>", elapsed_ms=1)])
    result = import_plan(session, client, date="2026-09-16", hours=[9])
    session.flush()
    assert result.buckets_fetched == 1

    # No responses queued: a second call to client.plan() would raise IndexError.
    client = _FakeClient([])
    result = import_plan(session, client, date="2026-09-16", hours=[9])
    assert result.buckets_skipped == 1
    assert result.buckets_fetched == 0
