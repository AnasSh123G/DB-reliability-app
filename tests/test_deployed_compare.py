"""The parity check finds no difference on a correct deployed copy, and reports one it should."""

from __future__ import annotations

import sqlite3
from datetime import date

import ml_fixture
import pytest
from sqlalchemy.orm import Session

from dbrel.db import make_engine
from dbrel.deployed_build import build_deployed
from dbrel.deployed_compare import compare, requests_for
from dbrel.web import queries


@pytest.fixture(scope="module")
def databases(tmp_path_factory):
    """Paths of a fixture collection database and its deployed copy."""
    folder = tmp_path_factory.mktemp("compare")
    ml_fixture.build(folder / "collection.sqlite")
    ml_fixture.add_station_alias(folder / "collection.sqlite")
    build_deployed(folder / "collection.sqlite", folder / "deployed.sqlite", log=lambda m: None)
    return folder / "collection.sqlite", folder / "deployed.sqlite"


def _requests(session):
    """The parity requests for every fixture station, train, line and line-less train."""
    day = date.fromisoformat(ml_fixture.DATES[0])
    return requests_for(session, now=ml_fixture.utc_of(ml_fixture.DATES[0], 8 * 3600 + 1800), service_date=day,
                        stations=[1, 2, 3, 4, 5], trains=[70, 71, 10, 11, 12], lines=[7], corridors=[10, 11],
                        terms=["Köln", "RE 7", "ICE"])


def test_a_correct_copy_answers_every_request_identically(databases):
    """A correct copy gives identical responses to every request."""
    collection, deployed = (make_engine(p) for p in databases)
    with Session(deployed) as s:
        requests = _requests(s)
    labels = " ".join(r.label for r in requests)
    assert "line 7 around" in labels and "corridor 10 around" in labels and "board 2" in labels
    assert compare(collection, deployed, requests, log=lambda m: None) == []


def test_a_changed_platform_is_reported_with_its_path(databases, tmp_path):
    """A changed platform in the copy is reported with its JSON path."""
    collection, deployed = databases
    broken = tmp_path / "broken.sqlite"
    con = sqlite3.connect(deployed)
    con.execute(f"VACUUM INTO '{broken}'")
    con.close()
    con = sqlite3.connect(broken)
    con.execute("UPDATE pattern_stop SET platform_code = '9' WHERE station_id = 2")
    con.commit()
    con.close()
    with Session(make_engine(broken)) as s:
        requests = _requests(s)
    differences = compare(make_engine(collection), make_engine(broken), requests, log=lambda m: None)
    assert differences
    assert any("platform" in d and "'9'" in d for d in differences)
    assert any(d.startswith("board 2:") for d in differences)


def test_a_copy_without_a_retired_trains_rows_still_answers_identically(tmp_path):
    """With ICE 12 retired, its route (the retired message) and every other request match."""
    ml_fixture.build(tmp_path / "collection.sqlite")
    ml_fixture.add_station_alias(tmp_path / "collection.sqlite")
    con = sqlite3.connect(tmp_path / "collection.sqlite")
    con.execute("UPDATE train SET last_seen_date = '2026-08-01' WHERE id = 12")
    con.execute("UPDATE train SET last_seen_date = '2026-09-10' WHERE id = 70")
    con.commit()
    con.close()
    build_deployed(tmp_path / "collection.sqlite", tmp_path / "deployed.sqlite", log=lambda m: None)
    collection, deployed = make_engine(tmp_path / "collection.sqlite"), make_engine(tmp_path / "deployed.sqlite")
    with Session(deployed) as s:
        requests = _requests(s)
        assert queries.train_route(s, 12)["retired"] == {"last_seen_date": "2026-08-01"}
    assert compare(collection, deployed, requests, log=lambda m: None) == []


def test_plans_use_monday_eight_include_both_directions_and_omit_stats(monkeypatch):
    """Plan parity compares the stable payload for sampled and explicit pairs."""
    from dbrel.config import TZ
    from datetime import datetime

    calls = []

    def plan(session, origin, target, epoch, day, clock):
        calls.append((origin, target, epoch, day, clock))
        return 200, {"journeys": [], "reason": "no_connection", "stats": {"ms": len(calls)}}

    monkeypatch.setattr(queries, "plan", plan, raising=False)
    requests = requests_for(None, now=0, service_date=date(2026, 10, 22),
                            stations=(), plans=[(41, 42)])
    assert [r.label for r in requests] == ["plan 41>42", "plan 42>41"]
    assert requests[0].call(None) == {"journeys": [], "reason": "no_connection"}
    assert requests[1].call(None) == {"journeys": [], "reason": "no_connection"}
    assert calls == [(41, 42, int(datetime(2026, 10, 19, 8, tzinfo=TZ).timestamp()),
                      "2026-10-19", "08:00"),
                     (42, 41, int(datetime(2026, 10, 19, 8, tzinfo=TZ).timestamp()),
                      "2026-10-19", "08:00")]


def test_sampled_station_plans_cover_three_pairs(monkeypatch):
    monkeypatch.setattr(queries, "station_board", lambda *a, **k: {})
    requests = requests_for(None, now=0, service_date=date(2026, 10, 22), stations=[1, 2, 3, 4])
    assert [r.label for r in requests if r.label.startswith("plan ")] == [
        "plan 1>2", "plan 2>1", "plan 2>3", "plan 3>2", "plan 3>1", "plan 1>3"]
