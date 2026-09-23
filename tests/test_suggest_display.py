"""What the search dropdown shows: stop names under each line/train, and no
line whose "name" is only a generic category code IRIS echoed ("RE")."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import SearchAlias, ServiceLine, Train
from dbrel.web import queries


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add_all([
            ServiceLine(id=1, identity_key="RE/RE22", category="RE", line="RE22",
                        display_name="RE22", search_name="re22", operator="800767"),
            ServiceLine(id=2, identity_key="RE/RE", category="RE", line="RE",
                        display_name="RE", search_name="re", operator="800515"),
            ServiceLine(id=3, identity_key="RE/FEX", category="RE", line="FEX",
                        display_name="FEX", search_name="fex", operator="800158"),
        ])
        s.flush()
        s.add_all([
            Train(id=10, source=SOURCE_TT, identity_key="tts:RE/22501", category="RE",
                  train_number=22501, display_name="RE 22501", search_name="re22501",
                  line_id=1, origin_name="Euskirchen", dest_name="Trier Hbf",
                  route_path="Euskirchen|Kall|Gerolstein|Trier Hbf"),
            Train(id=11, source=SOURCE_TT, identity_key="tts:RE/22502", category="RE",
                  train_number=22502, display_name="RE 22502", search_name="re22502",
                  line_id=1, origin_name="Kall", dest_name="Gerolstein",
                  route_path="Kall|Gerolstein"),
            Train(id=20, source=SOURCE_TT, identity_key="tts:RE/10000", category="RE",
                  train_number=10000, display_name="RE 10000", search_name="re10000",
                  line_id=2, origin_name="Euskirchen", dest_name="Köln Messe/Deutz",
                  route_path="Euskirchen|Köln Messe/Deutz"),
            Train(id=30, source=SOURCE_TT, identity_key="tts:RE/21800", category="RE",
                  train_number=21800, display_name="RE 21800", search_name="re21800",
                  line_id=3, origin_name="Flughafen BER", dest_name="Berlin Hbf",
                  route_path="Flughafen BER|Berlin Hbf"),
        ])
        s.flush()
        s.add_all([
            SearchAlias(alias_norm="re22", alias_display="RE22", kind="line", line_id=1),
            SearchAlias(alias_norm="re", alias_display="RE", kind="line", line_id=2),
            SearchAlias(alias_norm="fex", alias_display="FEX", kind="line", line_id=3),
            SearchAlias(alias_norm="re22501", alias_display="RE 22501", kind="train", train_id=10),
        ])
        s.flush()
        yield s


def names(results):
    return [(r["kind"], r["name"]) for r in results]


def test_bare_category_line_is_not_offered(session):
    assert ("line", "RE") not in names(queries.suggest(session, "re"))


def test_named_service_without_a_number_is_kept(session):
    assert ("line", "FEX") in names(queries.suggest(session, "fex"))


def test_line_shows_the_endpoints_of_its_longest_route(session):
    line = next(r for r in queries.suggest(session, "re22") if r["kind"] == "line")
    assert line["stops"] == "Euskirchen – Trier Hbf"


def test_train_shows_its_own_endpoints(session):
    train = next(r for r in queries.suggest(session, "re22501") if r["kind"] == "train")
    assert train["stops"] == "Euskirchen – Trier Hbf"
