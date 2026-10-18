"""Small, local regressions for the backfill speed paths."""

from __future__ import annotations

from dataclasses import replace

import pytest
from datetime import datetime

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.ingest.runs import RunContext, RunCounters, tt_feed_version, upsert_run
from dbrel.models import Station, Train, Trip, TtPoll
from test_upsert_run_collision import _stop


def _forbid_orm_bulk_update(monkeypatch):
    from sqlalchemy.orm import bulk_persistence

    def fail(*_args, **_kwargs):
        raise AssertionError("hot path used SQLAlchemy ORM bulk UPDATE synchronization")

    monkeypatch.setattr(
        bulk_persistence.BulkUDCompileState,
        "_get_matched_objects_on_criteria",
        fail,
    )


def test_collision_update_refreshes_loaded_trip_without_orm_bulk_sync(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as session:
        feed_version_id = tt_feed_version(session)
        session.add(Station(
            id=1, natural_key="bochum@51.479,7.223", name="Bochum Hbf",
            lat=51.479, lon=7.223, eva=8000041,
        ))
        session.flush()
        context = RunContext(
            fv_id=feed_version_id,
            station_id_by_eva={8000041: 1},
            station_name_by_eva={8000041: "Bochum Hbf"},
            train_by_key={},
        )
        counters = RunCounters()
        first = _stop(daily_trip_id="123", start_stamp="2609160800")
        trip_id = upsert_run(session, context, counters, first.run_key, [first])
        session.flush()
        loaded = session.get(Trip, trip_id)
        initial_digest = loaded.path_sha1
        revised_event = replace(first.departure, planned_path="Bochum Hbf|Essen Hbf")
        revised = replace(first, daily_trip_id="124", departure=revised_event)
        assert revised.run_key != first.run_key

        _forbid_orm_bulk_update(monkeypatch)
        same_trip_id = upsert_run(session, context, counters, revised.run_key, [revised])
        session.flush()

        assert same_trip_id == trip_id
        assert loaded.path_sha1 != initial_digest
        assert loaded.path_sha1 == session.scalar(select(Trip.path_sha1).where(Trip.id == trip_id))


def test_backfill_index_migration_is_repeatable_and_query_plans_use_indexes(monkeypatch, tmp_path):
    from dbrel import db
    from dbrel import cli
    from dbrel.cli import app
    from typer.testing import CliRunner

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for index_name in (
            "ix_trip_src_date", "ix_trip_unfinalized",
            "ix_ttpoll_ok_time", "ix_ttpoll_ok_station_time",
        ):
            connection.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
        connection.execute(text(
            "INSERT INTO train (source, identity_kind, identity_key, n_runs_seen) "
            "VALUES ('tts','NUMBERED','plan-test',1)"
        ))
        train_id = connection.exec_driver_sql("SELECT id FROM train").scalar_one()
        connection.execute(text(
            "INSERT INTO trip (train_id,service_date,source,source_run_key,status,"
            "endpoints_tracked,n_polls_seen,finalized_at) "
            "SELECT :train_id, date('2026-01-01', '+' || value || ' day'), 'tts', "
            "'plan-' || value, 'SCHEDULED',0,0, "
            "CASE WHEN value % 3 = 0 THEN NULL ELSE 1 END "
            "FROM (WITH RECURSIVE seq(value) AS (SELECT 0 UNION ALL SELECT value+1 FROM seq WHERE value<299) "
            "SELECT value FROM seq)"
        ), {"train_id": train_id})
        connection.execute(text(
            "INSERT INTO tt_poll (sweep_id,station_id,eva,fetched_at,http_status,bytes,"
            "n_stops,n_matched,n_unmatched,n_changed,error) "
            "SELECT 1,NULL,8000001,value,CASE WHEN value % 4 = 0 THEN 503 ELSE 200 END,0,0,0,0,0, "
            "CASE WHEN value % 4 = 0 THEN 'failure' ELSE NULL END "
            "FROM (WITH RECURSIVE seq(value) AS (SELECT 1 UNION ALL SELECT value+1 FROM seq WHERE value<400) "
            "SELECT value FROM seq)"
        ))
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "migration.sqlite")
    runner = CliRunner()

    first = runner.invoke(app, ["migrate-backfill-indexes"])
    second = runner.invoke(app, ["migrate-backfill-indexes"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    names = {index["name"] for table in ("trip", "tt_poll")
             for index in inspect(engine).get_indexes(table)}
    assert {
        "ix_trip_src_date", "ix_trip_unfinalized",
        "ix_ttpoll_ok_time", "ix_ttpoll_ok_station_time",
    } <= names
    with engine.connect() as connection:
        connection.exec_driver_sql("ANALYZE")
        trip_plan = " ".join(str(row) for row in connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT id FROM trip WHERE source='tts' "
            "AND service_date IN ('2026-01-01','2026-01-02')"
        ))
        finalize_plan = " ".join(str(row) for row in connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT id FROM trip WHERE source='tts' "
            "AND service_date IN ('2026-01-01','2026-01-02') AND finalized_at IS NULL"
        ))
        unbounded_finalize_plan = " ".join(str(row) for row in connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT id FROM trip WHERE source='tts' AND finalized_at IS NULL"
        ))
    assert "ix_trip_src_date" in trip_plan
    assert "ix_trip_unfinalized" in finalize_plan
    assert "ix_trip_unfinalized" in unbounded_finalize_plan
    engine.dispose()


def _epoch(day: str, hour: int) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").timestamp())


def _trip(session, train_id, *, service_date, run_key, event):
    trip = Trip(
        train_id=train_id, service_date=service_date, source="tts",
        source_run_key=run_key, status="SCHEDULED", supposed_arrival_utc=event,
        supposed_departure_utc=event, finalized_at=None,
    )
    session.add(trip)
    session.flush()
    return trip


def test_bounded_finalize_gap_grades_match_old_global_poll_oracle(session, monkeypatch):
    from dbrel.config import GRACE_S, TT_LINGER_S
    from dbrel.ingest import finalize_tt
    from dbrel.ingest.dated_schedule import DatedPlanned
    from dbrel.models import TripStop

    train = Train(source="tts", identity_key="poll-oracle-train", display_name="RE 1")
    session.add(train)
    session.flush()
    base = 1_000_000
    trip = _trip(session, train.id, service_date="2026-05-10", run_key="poll-oracle",
                 event=base + 1)
    monkeypatch.setattr(finalize_tt, "_load_patterns", lambda *_args, **_kwargs: {
        (train.id, trip.service_date): {
            1: DatedPlanned(None, 1, None, None, base),
            2: DatedPlanned(None, 2, None, None, base + 600),
        }
    })
    polls = [
        TtPoll(sweep_id=2, station_id=1, eva=8000001, fetched_at=base + GRACE_S + 10,
               http_status=200),
        TtPoll(sweep_id=2, station_id=2, eva=8000002, fetched_at=base + 50,
               http_status=200),
        TtPoll(sweep_id=2, station_id=2, eva=8000002,
               fetched_at=base + 600 + TT_LINGER_S + 10, http_status=200),
    ]
    session.add_all(polls)
    session.flush()

    result = finalize_tt.run_finalize_tt(
        session, service_dates={trip.service_date},
        now=base + TT_LINGER_S + 2 * 60 * 60,
    )

    assert result.trips_considered == 1
    facts = {
        row.stop_sequence: row
        for row in session.execute(
            select(TripStop).where(TripStop.trip_id == trip.id)
        ).scalars()
    }
    # Oracle is the previous unbounded successful-poll list and global latest.
    by_station = {1: [base + GRACE_S + 10], 2: [base + 50, base + 600 + TT_LINGER_S + 10]}
    assert facts[1].quality == (
        "NO_DATA" if finalize_tt._polled_between(
            by_station[1], base + GRACE_S, base + TT_LINGER_S
        ) else "PREDICTION_ONLY"
    )
    assert facts[2].quality == (
        "NO_DATA" if finalize_tt._polled_between(
            by_station[2], base + 600 + GRACE_S, base + 600 + TT_LINGER_S
        ) else "PREDICTION_ONLY"
    )
    assert facts[2].poll_gap_s == max(
        0, base + 600 + TT_LINGER_S - by_station[2][-1]
    ) == 0


def test_replay_finalizes_pending_trips_across_missing_and_plan_only_day_gaps(
    session, monkeypatch, tmp_path,
):
    from datetime import date

    from dbrel.ingest import archive, finalize_tt, replay
    from dbrel.ingest.archive import API_FCHG, ArchivedResponse
    from dbrel.ingest.changes_poller import PollResult
    from dbrel.ingest.dated_schedule import DatedPlanned
    from dbrel.models import QUALITY_PREDICTION_ONLY, TripStop

    train = Train(source="tts", identity_key="catch-up-gap-train", display_name="RE 1")
    session.add(train)
    session.flush()
    service_dates = ["2026-05-03", "2026-05-05", "2026-05-08"]
    now = _epoch("2026-05-10", 23)
    trips = [
        _trip(session, train.id, service_date=service_date, run_key=f"catchup-{service_date}",
              event=now - 86_400)
        for service_date in service_dates
    ]
    monkeypatch.setattr(finalize_tt, "_load_patterns", lambda *_args, **_kwargs: {
        (train.id, trip.service_date): {
            1: DatedPlanned(None, 1, None, None, now - 86_400),
        }
        for trip in trips
    })
    # The replayed day has one changes sweep. Earlier dates model a missing
    # archive day (May 3), a plan-only/no-fchg day (May 5), and a multi-day
    # delay before the next available evidence (May 8 -> May 10).
    day = date(2026, 5, 10)
    response = ArchivedResponse(
        recorded_at=now, api_name=API_FCHG, eva=8000001, body=b"<timetable/>"
    )
    partition = tmp_path / "isolated-partition.parquet"
    partition.touch()
    monkeypatch.setattr(archive, "files_for_date", lambda _store, _day: [partition])
    monkeypatch.setattr(archive, "missing_hours", lambda *_args: [])
    monkeypatch.setattr(archive, "read_partition", lambda _path: iter([response]))
    monkeypatch.setattr(archive, "covered_hours", lambda _paths: {(day, 0)})
    monkeypatch.setattr(
        "dbrel.ingest.changes_poller.run_tt_poll",
        lambda *_args, **_kwargs: PollResult(sweep_id=1),
    )

    result = replay.replay_day(
        session, object(), day, tracked_evas={8000001}, progress=lambda _message: None,
    )

    assert result.sweeps == 1
    assert result.trips_finalized == 3
    assert result.gaps_written == 3
    assert result.by_quality[QUALITY_PREDICTION_ONLY] == 3
    persisted = session.execute(
        select(TripStop.trip_id, TripStop.quality).order_by(TripStop.trip_id)
    ).all()
    assert len(persisted) == 3
    assert {quality for _trip_id, quality in persisted} == {QUALITY_PREDICTION_ONLY}


def _pragma(engine, name):
    with engine.connect() as connection:
        return connection.exec_driver_sql(f"PRAGMA {name}").scalar_one()


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as current:
        from test_archive_replay import PUNCTUAL_EVA, SILENT_EVA

        current.add_all([
            Station(natural_key="dormagen@51.099,6.837", name="Dormagen",
                    lat=51.099, lon=6.837, eva=PUNCTUAL_EVA, tracked=1),
            Station(natural_key="lennestadt-meggen@51.116,8.053", name="Lennestadt-Meggen",
                    lat=51.116, lon=8.053, eva=SILENT_EVA, tracked=1),
        ])
        current.commit()
        yield current
