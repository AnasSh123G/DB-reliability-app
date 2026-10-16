"""Measure the deployed planner; run only through the capped ML launcher."""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from time import monotonic

from sqlalchemy import text
from sqlalchemy.orm import Session

from dbrel.config import DB_PATH, TZ
from dbrel.db import make_engine
from dbrel.planner.timetable import get_timetable
from dbrel.web import queries

PAIRS = (
    ("Köln Hbf", "Berlin Hbf"),
    ("München Hbf", "Hamburg Hbf"),
    ("Aachen Hbf", "Dortmund Hbf"),
    ("Bochum Hbf", "Essen Hbf"),
    ("Freiburg(Breisgau) Hbf", "Kiel Hbf"),
    ("Frankfurt(Main)Hbf", "Stuttgart Hbf"),
)


def memory(stage):
    values = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/self/status").read_text().splitlines()
        if line.startswith(("VmRSS:", "VmSwap:"))
    }
    result = {
        "stage": stage,
        "rss_kib": values.get("VmRSS", 0),
        "swap_kib": values.get("VmSwap", 0),
    }
    result["total_kib"] = result["rss_kib"] + result["swap_kib"]
    print("MEMORY " + json.dumps(result), flush=True)
    return result


def resolve_station(session, name):
    row = session.execute(
        text("SELECT id, name FROM station WHERE name = :name ORDER BY id LIMIT 1"),
        {"name": name},
    ).first()
    if row is None:
        # Names vary between feeds: Aachen, Hbf and Frankfurt (Main) Hauptbahnhof.
        prefix = re.split(r"[\s(,]", name)[0]
        candidates = session.execute(
            text(
                "SELECT id, name FROM station WHERE name LIKE :name ORDER BY name, id"
            ),
            {"name": prefix + "%"},
        ).all()

        def compact(value):
            return re.sub(r"\W", "", value).casefold().replace("hbf", "hauptbahnhof")

        row = next(
            (
                candidate
                for candidate in candidates
                if compact(candidate.name) == compact(name)
            ),
            candidates[0] if candidates else None,
        )
    if row is None:
        raise RuntimeError(f"No station matching {name!r}")
    print(f"Station {name!r}: {row.name!r} (id={row.id})", flush=True)
    return row


def main():
    today = datetime.now(TZ).date()
    day = today + timedelta(days=(1 - today.weekday()) % 7 or 7)
    at = int(datetime.combine(day, time(8), TZ).timestamp())
    print(f"Planner benchmark database={DB_PATH} date={day} time=08:00", flush=True)
    memory("before_loading")
    engine = make_engine(DB_PATH)
    try:
        with Session(engine) as session:
            start = monotonic()
            tt = get_timetable(session)
            print(
                "INDEX "
                + json.dumps({"seconds": monotonic() - start, "nbytes": tt.nbytes()}),
                flush=True,
            )
            memory("after_index")
            for origin, destination in PAIRS:
                source, target = (
                    resolve_station(session, origin),
                    resolve_station(session, destination),
                )
                for phase in ("cold", "warm"):
                    start = monotonic()
                    status, response = queries.plan(
                        session, source.id, target.id, at, day.isoformat(), "08:00"
                    )
                    journeys = response.get("journeys", [])
                    result = {
                        "from": source.name,
                        "to": target.name,
                        "phase": phase,
                        "status": status,
                        "journeys": len(journeys),
                        "reason": response.get("reason"),
                        "stats": response.get("stats"),
                        "seconds": monotonic() - start,
                        "error": response.get("error"),
                    }
                    if journeys:
                        result["first"] = {
                            key: journeys[0][key]
                            for key in ("route_key", "departure", "expected_arrival")
                        }
                        result["legs"] = journeys[0]["legs"]
                    print("PLAN " + json.dumps(result, ensure_ascii=False), flush=True)
            memory("after_all_plans")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
