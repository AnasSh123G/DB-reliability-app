"""Passenger lines: trains regrouped into the lines passengers know.

service_line is keyed by category + line text, so it both merges unrelated
lines (every "RE"+"4" in Germany) and splits one real line ("RE/4" and
"NX/RE4" for the same Aachen-Dortmund RE4).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.derive.passenger_lines import build_passenger_lines
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    PassengerLine,
    PassengerLineMember,
    SearchAlias,
    ServiceLine,
    Train,
)

AACHEN_DORTMUND = ["Aachen Hbf", "Mönchengladbach Hbf", "Düsseldorf Hbf", "Wuppertal Hbf",
                   "Hagen Hbf", "Dortmund Hbf"]
SCHWERIN_SZCZECIN = ["Schwerin Hbf", "Bad Kleinen", "Pasewalk", "Szczecin Glowny"]


def _train(s, train_id, line_id, path, *, category="RE", last_seen="2026-09-28"):
    s.add(Train(
        id=train_id, source=SOURCE_TT, identity_key=f"tts:{category}/{train_id}",
        category=category, segment="RV", train_number=train_id, operator="X",
        display_name=f"{category} {train_id}", line_id=line_id,
        origin_name=path[0], dest_name=path[-1], n_stops=len(path),
        route_path="|".join(path), path_sha1=f"main{train_id}", last_seen_date=last_seen,
    ))


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add_all([
            # One "RE/4" row for every RE+4 in Germany ...
            ServiceLine(id=1, identity_key="RE/4", category="RE", line="4",
                        display_name="RE 4", search_name="re4"),
            # ... and the Aachen RE4 a second time, written differently.
            ServiceLine(id=2, identity_key="NX/RE4", category="NX", line="RE4",
                        display_name="RE4", search_name="re4"),
            ServiceLine(id=3, identity_key="Bus/RE4", category="Bus", line="RE4",
                        display_name="RE4", search_name="re4"),
            ServiceLine(id=4, identity_key="RE/RE", category="RE", line="RE",
                        display_name="RE", search_name="re"),
        ])
        s.flush()
        _train(s, 100, 1, AACHEN_DORTMUND)
        _train(s, 101, 2, AACHEN_DORTMUND[::-1], category="NX")
        _train(s, 102, 2, AACHEN_DORTMUND[:4], category="NX")  # a short working
        _train(s, 200, 1, SCHWERIN_SZCZECIN)
        _train(s, 300, 3, AACHEN_DORTMUND[:3], category="Bus")
        _train(s, 400, 4, AACHEN_DORTMUND)  # bare "RE": names no line
        _train(s, 103, 2, AACHEN_DORTMUND, category="NX", last_seen="2025-12-13")
        s.add_all([
            SearchAlias(alias_norm="re4", alias_display="RE 4", kind="CAT_LINE", weight=90,
                        line_id=1),
            SearchAlias(alias_norm="re4", alias_display="RE4", kind="CAT_LINE", weight=90,
                        line_id=2),
            SearchAlias(alias_norm="re4", alias_display="RE4", kind="CAT_LINE", weight=80,
                        line_id=3),
        ])
        s.flush()
        build_passenger_lines(s)
        s.flush()
        yield s


def _line_of(session, train_id):
    return session.get(PassengerLineMember, train_id).passenger_line_id


def test_one_real_line_written_two_ways_is_one_passenger_line(session):
    assert _line_of(session, 100) == _line_of(session, 101) == _line_of(session, 102)


def test_the_same_name_in_another_region_is_another_line(session):
    assert _line_of(session, 200) != _line_of(session, 100)


def test_a_replacement_bus_on_the_same_stretch_stays_its_own_line(session):
    assert _line_of(session, 300) != _line_of(session, 100)
    assert session.get(PassengerLine, _line_of(session, 300)).category == "Bus"


def test_the_line_id_is_its_smallest_train_id_so_urls_survive_a_rebuild(session):
    aachen = _line_of(session, 100)
    assert aachen == 100
    build_passenger_lines(session)
    assert _line_of(session, 101) == aachen


