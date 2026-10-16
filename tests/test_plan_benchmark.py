"""Measurement pairs resolve to central stations despite timetable spelling variants."""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from plan_benchmark import resolve_station


@pytest.mark.parametrize(
    "requested,expected", [("Aachen Hbf", 2), ("Frankfurt(Main)Hbf", 141)]
)
def test_benchmark_resolves_central_station_spelling(requested, expected):
    with Session(create_engine("sqlite://")) as session:
        session.execute(text("CREATE TABLE station(id INTEGER PRIMARY KEY, name TEXT)"))
        session.execute(
            text("INSERT INTO station VALUES (:id,:name)"),
            [
                {"id": 1, "name": "Aachen Schanz"},
                {"id": 2, "name": "Aachen, Hbf"},
                {"id": 3, "name": "Frankfurt (Main) Berkersheim Bahnhof"},
                {"id": 4, "name": "Frankfurt (Oder) Hauptbahnhof"},
                {"id": 141, "name": "Frankfurt (Main) Hauptbahnhof"},
                {"id": 142, "name": "Frankfurt (Main) Hauptbahnhof tief"},
            ],
        )
        assert resolve_station(session, requested).id == expected
