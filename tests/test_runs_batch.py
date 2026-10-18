"""Differential tests: the batched plan writer must equal the per-run writer (D7).

One synthetic `import_plan` input is imported into two fresh file databases, once
with `writer="per_run"` and once with `writer="batch"`, and the two are compared
with `ingest_compare` and no allow-list. The scenarios cover every case the D7
design lists, including the fingerprint drift, which the batch must reproduce
rather than fix.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.ingest.client import Response
from dbrel.ingest.dated_schedule import PlanReceipt
from dbrel.ingest.plan_import import import_plan
from dbrel.ingest.replay import ReplayDayContext
from dbrel.ingest.runs import RunContext, RunCounters, tt_feed_version, upsert_run
from dbrel.ingest_compare import compare_databases
from dbrel.models import Station, Train, Trip, TripScheduleSnapshot, TripStop
from dbrel.identity import SOURCE_TT
from test_upsert_run_collision import _stop

BOCHUM, DORTMUND, ESSEN = 8000041, 8000080, 8000098
# Dortmund's stored name deliberately differs from IRIS's own spelling in ppth.
STATIONS = {BOCHUM: "Bochum Hbf", DORTMUND: "Dortmund Hauptbahnhof", ESSEN: "Essen Hbf"}
NOW = 1_800_000_000


def _s(dtid, stamp, idx, *, number, owner="DB", ar=None, dp=None, category="RE"):
    """One `<s>` element. `ar`/`dp` are (pt, ppth) pairs; neither gives a bare stop."""
    parts = [f'<s id="{dtid}-{stamp}-{idx}"><tl t="p" o="{owner}" c="{category}" n="{number}"/>']
    if ar:
        parts.append(f'<ar pt="{ar[0]}" pp="1" l="RE1" ppth="{ar[1]}"/>')
    if dp:
        parts.append(f'<dp pt="{dp[0]}" pp="1" l="RE1" ppth="{dp[1]}"/>')
    parts.append("</s>")
    return "".join(parts)


class _Client:
    """Answers `plan(eva, yymmdd, hour)` from {(eva, hour): [<s> elements]}."""

    def __init__(self, buckets):
        self._buckets = buckets

    def plan(self, eva, date, hour):
        stops = self._buckets.get((eva, hour))
        if not stops:
            return Response(status=404, body=b"", elapsed_ms=0)
        name = STATIONS[eva]
        body = f'<timetable station="{name}" eva="{eva}">{"".join(stops)}</timetable>'.encode()
        return Response(status=200, body=body, elapsed_ms=0)


def _first_call():
    """Hour 8: new train, new date, other run key on a trip's date, variants."""
    b: dict = {}

    def add(eva, hour, xml):
        b.setdefault((eva, hour), []).append(xml)

    for stamp, dtid in (("2609160800", "R1"), ("2609170800", "R1"), ("2609160800", "R1X")):
        kw = dict(number="1001")
        add(BOCHUM, 8, _s(dtid, stamp, 1, dp=("2609160801", "Dortmund Hbf|Essen Hbf"), **kw))
        add(DORTMUND, 8, _s(dtid, stamp, 2, ar=("2609160815", "Bochum Hbf"),
                            dp=("2609160816", "Essen Hbf"), **kw))
        add(ESSEN, 8, _s(dtid, stamp, 3, ar=("2609160830", "Bochum Hbf|Dortmund Hbf"), **kw))
    # a numbered train with a different path AND operator -> '#digest' variant
    add(BOCHUM, 8, _s("R2", "2609160900", 1, number="1002", dp=("2609160901", "Essen Hbf")))
    add(BOCHUM, 8, _s("R2V", "2609160900", 1, number="1002", owner="XX",
                      dp=("2609160901", "Dortmund Hbf")))
    # same number and path, different operator -> no variant
    add(BOCHUM, 8, _s("R3", "2609161000", 1, number="1003", dp=("2609161001", "Essen Hbf")))
    add(BOCHUM, 8, _s("R3B", "2609161000", 1, number="1003", owner="XX",
                      dp=("2609161001", "Essen Hbf")))
    return b


def _second_call():
    """Hours 10-13: pinned check, duplicate, bare stop, DST, re-point, drift, facts."""
    b: dict = {}

    def add(eva, hour, xml):
        b.setdefault((eva, hour), []).append(xml)

    # R1 re-observed with a changed plan time: reconcile runs against the seeded fact.
    add(BOCHUM, 10, _s("R1", "2609160800", 1, number="1001",
                       dp=("2609160803", "Dortmund Hbf|Essen Hbf")))
    # a station visited twice: the pinned-index check
    add(BOCHUM, 10, _s("R4", "2609161000", 1, number="1004", dp=("2609161001", "Dortmund Hbf|Bochum Hbf")))
    add(BOCHUM, 10, _s("R4", "2609161000", 3, number="1004", ar=("2609161030", "Bochum Hbf|Dortmund Hbf")))
    add(DORTMUND, 10, _s("R4", "2609161000", 2, number="1004", ar=("2609161015", "Bochum Hbf"),
                         dp=("2609161016", "Bochum Hbf")))
    # a duplicate observation: the same bucket body in two hours
    for hour in (10, 11):
        add(BOCHUM, hour, _s("R5", "2609161100", 1, number="1005", dp=("2609161101", "Essen Hbf")))
    # an observation with neither arrival nor departure
    add(BOCHUM, 11, _s("R6", "2609161100", 1, number="1006"))
    # a DST-ambiguous stop: the second 02:xx comes after 02:30
    add(BOCHUM, 11, _s("R7", "2610250200", 1, number="1007", dp=("2610250230", "Dortmund Hbf")))
    add(DORTMUND, 11, _s("R7", "2610250200", 2, number="1007", ar=("2610250215", "Bochum Hbf")))
    # a trip re-pointed mid-batch while dirty: same train and date, new run key, new path
    add(BOCHUM, 12, _s("R8A", "2609161200", 1, number="1008", dp=("2609161201", "Dortmund Hbf")))
    add(BOCHUM, 12, _s("R8B", "2609161200", 1, number="1008", dp=("2609161201", "Essen Hbf")))
    # the drift case: the same physical route, but the head stop is a different
    # station whose stored name differs from IRIS's spelling
    add(BOCHUM, 13, _s("R9A", "2609161300", 1, number="1009", dp=("2609161301", "Dortmund Hbf")))
    add(DORTMUND, 13, _s("R9B", "2609171300", 2, number="1009", ar=("2609171315", "Bochum Hbf"),
                         dp=("2609171316", "Essen Hbf")))
    return b


def _build(path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    for eva, name in STATIONS.items():
        session.add(Station(natural_key=f"{eva}", name=name, lat=51.0, lon=7.0, eva=eva, tracked=1))
    session.flush()
    return engine, session


def _ingest(path: Path, writer: str):
    engine, session = _build(path)
    try:
        day_ctx = ReplayDayContext(
            fv_id=tt_feed_version(session),
            train_by_key={},
            line_cache={},
            tracked_stations=list(session.scalars(select(Station))),
        )
        results = []
        results.append(import_plan(
            session, _Client(_first_call()), date="2026-09-16", hours=[8], force=True,
            workers=1, now=NOW, day_ctx=day_ctx, writer=writer,
        ))
        session.flush()
        # A recorded fact for R1's first stop, so reconcile has something to settle.
        trip = session.scalar(select(Trip).where(
            Trip.source_run_key == "R1-2609160800", Trip.service_date == "2026-09-16"))
        bochum = session.scalar(select(Station).where(Station.eva == BOCHUM))
        session.add(TripStop(
            trip_id=trip.id, stop_sequence=1, station_id=bochum.id,
            actual_departure_utc=NOW + 120, quality="RECORDED", rule_version=3, cancelled=0,
        ))
        session.flush()
        results.append(import_plan(
            session, _Client(_second_call()), date="2026-09-16", hours=[10, 11, 12, 13],
            force=True, workers=1, now=NOW + 60, day_ctx=day_ctx, writer=writer,
        ))
        # The synthetic feed row is stamped with the wall clock; nothing else is.
        session.execute(text("UPDATE feed_version SET downloaded_at = 0"))
        session.commit()
        return results
    finally:
        session.close()
        engine.dispose()


def _comparable(result):
    data = asdict(result)
    data.pop("elapsed_s")
    data.pop("batch_epochs")  # only the batch writer counts epochs
    return data


def test_batch_writer_matches_per_run_writer_across_every_case(tmp_path):
    reference, batched = tmp_path / "per_run.sqlite", tmp_path / "batch.sqlite"
    expected = _ingest(reference, "per_run")
    actual = _ingest(batched, "batch")

    for result in (*expected, *actual):
        assert result.errors == []
    assert [_comparable(r) for r in actual] == [_comparable(r) for r in expected]

    report = compare_databases(reference, batched)
    assert report["identical"], {
        table: (info["only_a"], info["only_b"], info["differing"], info["samples"][:1])
        for table, info in report["tables"].items()
        if info["only_a"] or info["only_b"] or info["differing"]
    }


def test_scenario_exercises_every_designed_case(tmp_path):
    """Guards the fixture: a differential over an empty database proves nothing."""
    path = tmp_path / "per_run.sqlite"
    _ingest(path, "per_run")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    with Session(engine) as db:
        keys = set(db.scalars(select(Train.identity_key).where(Train.source == SOURCE_TT)))
        assert any("#" in key for key in keys), "variant identity expected"
        # distinct digests, including the drifted head spelling
        assert len(set(db.scalars(select(Train.path_sha1)))) >= 5
        assert db.scalar(select(func.count()).select_from(Trip)) >= 8
        assert db.scalar(select(func.count()).select_from(TripScheduleSnapshot)) > 0
        fact = db.scalar(select(TripStop).where(TripStop.stop_sequence == 1))
        assert fact is not None
    engine.dispose()


def test_batch_writer_raises_the_same_error_for_an_unmapped_eva(tmp_path):
    from dbrel.ingest.runs_batch import upsert_runs_batch
    from dbrel.ingest.schedule_reconcile import PendingSettle

    def attempt(batch):
        engine, session = _build(tmp_path / f"unmapped-{batch}.sqlite")
        try:
            station = session.scalar(select(Station).where(Station.eva == BOCHUM))
            ctx = RunContext(
                fv_id=tt_feed_version(session),
                station_id_by_eva={BOCHUM: station.id},
                station_name_by_eva={BOCHUM: station.name},
                train_by_key={},
            )
            stop = _stop(daily_trip_id="unmapped", start_stamp="2609160800")
            stop = replace(stop, eva=8099999)
            receipt = PlanReceipt(observed_at=NOW, content_sha256="a" * 64)
            with pytest.raises(ValueError) as caught:
                if batch:
                    upsert_runs_batch(
                        session, ctx, RunCounters(),
                        [(stop.run_key, [stop], [(stop, receipt)])], pending=PendingSettle(),
                    )
                else:
                    upsert_run(session, ctx, RunCounters(), stop.run_key, [stop],
                               schedule_observations=[(stop, receipt)])
            return str(caught.value)
        finally:
            session.close()
            engine.dispose()

    assert attempt(True) == attempt(False) == "observation EVA has no station mapping"
