"""Station.state resolution from coordinates."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.derive.station_states import resolve_station_states
from dbrel.models import Station


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Station(id=1, natural_key="a", name="München Hbf", lat=48.140232, lon=11.558335))
        s.add(Station(id=2, natural_key="b", name="Hamburg Hbf", lat=53.552736, lon=10.006909))
        s.add(Station(id=3, natural_key="c", name="Somewhere in Paris", lat=48.8566, lon=2.3522))
        s.flush()
        yield s


def test_resolves_known_stations_and_reports_the_unresolved_one(session):
    report = resolve_station_states(session)
    assert report.total == 3
    assert report.per_state == {"DE-BY": 1, "DE-HH": 1}
    assert report.unresolved == 1
    assert "Somewhere in Paris" in report.unresolved_names

    munich = session.get(Station, 1)
    hamburg = session.get(Station, 2)
    paris = session.get(Station, 3)
    assert munich.state == "DE-BY"
    assert hamburg.state == "DE-HH"
    assert paris.state is None


def test_rerunning_is_idempotent(session):
    resolve_station_states(session)
    first = {s.id: s.state for s in session.execute(select(Station)).scalars().all()}
    resolve_station_states(session)
    second = {s.id: s.state for s in session.execute(select(Station)).scalars().all()}
    assert first == second
