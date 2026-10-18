"""Small, local regressions for the backfill speed paths."""

from __future__ import annotations

from dataclasses import replace

import pytest
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.ingest.runs import RunContext, RunCounters, tt_feed_version, upsert_run
from dbrel.models import Station, Train, Trip, TtPoll
from test_archive_replay import AFTER, _Source, _replay
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


def test_second_same_session_poll_updates_loaded_trip_without_orm_bulk_sync(session, monkeypatch):
    _replay(session, now=AFTER)
    trip = session.scalar(select(Trip).limit(1))
    assert trip is not None
    previous_polls = trip.n_polls_seen
    previous_status = trip.status

    _forbid_orm_bulk_update(monkeypatch)
    from dbrel.ingest.changes_poller import run_tt_poll

    result = run_tt_poll(session, _Source(), priority_max=99, now=AFTER + 60)
    session.flush()

    assert result.sweep_id > 0
    assert trip.n_polls_seen == previous_polls + 1
    assert previous_status == "RUNNING"
    assert trip.status == "RUNNING"


def test_create_all_declares_the_four_backfill_indexes(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'indexes.sqlite'}", future=True)
    Base.metadata.create_all(engine)

    indexes = {
        (index["name"], tuple(index["column_names"]))
        for table in ("trip", "tt_poll")
        for index in inspect(engine).get_indexes(table)
    }
    names = {name for name, _columns in indexes}
    assert {
        "ix_trip_src_date", "ix_trip_unfinalized",
        "ix_ttpoll_ok_time", "ix_ttpoll_ok_station_time",
    } <= names
    partial_sql = {
        name: sql for name, sql in engine.connect().execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
        ))
    }
    assert "WHERE finalized_at IS NULL" in partial_sql["ix_trip_unfinalized"]
    assert "WHERE error IS NULL AND http_status = 200" in partial_sql["ix_ttpoll_ok_time"]
    assert "WHERE error IS NULL AND http_status = 200" in partial_sql["ix_ttpoll_ok_station_time"]
    engine.dispose()


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


def test_replay_finalize_date_bound_keeps_day_before_crossing_and_time_bound(session, monkeypatch):
    from dbrel.ingest import finalize_tt

    monkeypatch.setattr(finalize_tt, "_load_patterns", lambda *_args, **_kwargs: {})
    train = Train(source="tts", identity_key="finalize-bound-train", display_name="RE 1")
    session.add(train)
    session.flush()
    now = _epoch("2026-05-10", 23)
    _trip(session, train.id, service_date="2026-05-09", run_key="cross-midnight",
          event=_epoch("2026-05-10", 1))
    _trip(session, train.id, service_date="2026-05-10", run_key="same-day-old",
          event=_epoch("2026-05-10", 20))
    _trip(session, train.id, service_date="2026-05-08", run_key="older-service-date",
          event=_epoch("2026-05-08", 20))
    future_train = Train(source="tts", identity_key="future-finalize-bound-train")
    session.add(future_train)
    session.flush()
    _trip(session, future_train.id, service_date="2026-05-10", run_key="outside-time-bound",
          event=now + 3600)

    bounded = finalize_tt.run_finalize_tt(
        session, service_dates={"2026-05-09", "2026-05-10"}, now=now,
    )
    assert bounded.trips_considered == 2

    # The long-standing service_date= API intentionally remains unbounded by time.
    legacy = finalize_tt.run_finalize_tt(session, service_date="2026-05-10", now=now)
    assert legacy.trips_considered == 2


def test_bounded_poll_times_match_global_latest_poll_oracle(session):
    from dbrel.ingest.finalize_tt import _polled_between, _poll_times_by_station

    session.add_all([
        Station(id=3, natural_key="poll-test-three", name="Poll test 3", lat=0, lon=0),
        Station(id=4, natural_key="poll-test-four", name="Poll test 4", lat=0, lon=0),
    ])
    session.flush()
    session.add_all([
        TtPoll(sweep_id=1, station_id=3, eva=8000003, fetched_at=50, http_status=200),
        TtPoll(sweep_id=1, station_id=3, eva=8000003, fetched_at=120, http_status=200),
        TtPoll(sweep_id=1, station_id=3, eva=8000003, fetched_at=500, http_status=200),
        TtPoll(sweep_id=1, station_id=3, eva=8000003, fetched_at=700, http_status=503),
        TtPoll(sweep_id=1, station_id=4, eva=8000004, fetched_at=900, http_status=200,
               error="failed response"),
    ])
    session.flush()

    bounded, latest_by_station = _poll_times_by_station(session, since=100)

    assert bounded == {3: [120, 500]}
    assert latest_by_station == {3: 500}
    # Same coverage and poll-gap facts as the prior unbounded station list,
    # including a successful poll after the last relevant trip's window.
    old_global = [50, 120, 500]
    assert _polled_between(old_global, 100, 200) == _polled_between(bounded[3], 100, 200)
    reference, linger = 100, 600
    assert max(0, reference + linger - old_global[-1]) == max(
        0, reference + linger - latest_by_station[3]
    )


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


def test_bulk_load_pragmas_are_temporary_and_normal_pragmas_are_restored(tmp_path, monkeypatch):
    from dbrel import config, db

    monkeypatch.setattr(config, "BACKFILL_CACHE_MIB", 2048, raising=False)
    monkeypatch.setattr(config, "BACKFILL_MMAP_MIB", 8192, raising=False)
    monkeypatch.setattr(db, "_BULK_LOAD", False, raising=False)
    path = tmp_path / "pragma-test.sqlite"
    normal = db.make_engine(path)
    assert _pragma(normal, "cache_size") == -131072
    assert _pragma(normal, "mmap_size") == 268435456
    normal.dispose()

    db.enable_bulk_load()
    assert db._BULK_LOAD is True
    bulk = db.make_engine(path)
    assert _pragma(bulk, "cache_size") == -(2048 * 1024)
    bulk_mmap = _pragma(bulk, "mmap_size")
    with bulk.connect() as connection:
        options = connection.exec_driver_sql("PRAGMA compile_options").scalars().all()
    limit_option = next((option.split("=", 1)[1] for option in options
                         if option.startswith("MAX_MMAP_SIZE=")), None)
    sqlite_limit = int(limit_option, 0) if limit_option is not None else 8192 * 1024 * 1024
    assert bulk_mmap == min(8192 * 1024 * 1024, sqlite_limit)
    assert _pragma(bulk, "wal_autocheckpoint") == 20000
    bulk.dispose()

    db.enable_bulk_load(enabled=False)
    assert db._BULK_LOAD is False
    restored = db.make_engine(path)
    assert _pragma(restored, "cache_size") == -131072
    assert _pragma(restored, "mmap_size") == 268435456
    restored.dispose()


def test_backfill_restores_bulk_pragmas_after_early_exit(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import typer

    from dbrel import db
    from dbrel.cli import backfill

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cli-bulk.sqlite'}", future=True)
    Base.metadata.create_all(engine)

    @contextmanager
    def isolated_session_scope():
        with Session(engine, future=True) as current:
            yield current

    from dbrel import cli

    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "cli-bulk.sqlite")
    monkeypatch.setattr(cli, "TT_SWEEP_LOCK", tmp_path / "tt_sweep.lock")
    monkeypatch.setattr(cli, "session_scope", isolated_session_scope)
    monkeypatch.setattr(cli, "create_all", lambda: None)
    original_enable = db.enable_bulk_load
    states = []

    def track_bulk_state(enabled=True):
        states.append(enabled)
        return original_enable(enabled)

    monkeypatch.setattr(db, "enable_bulk_load", track_bulk_state)

    with pytest.raises(typer.Exit):
        backfill(
            date_from="2026-05-10", date_to="2026-05-10",
            days=30, root=tmp_path / "empty-archive", dry_run=False,
            done_file=None, timings=False, restart=False, bulk_pragmas=True, merge_plan_hours=False, plan_writer="per_run",
        )

    assert states == [True, False]
    assert db._BULK_LOAD is False
    engine.dispose()


@pytest.mark.parametrize("flag", ["--bulk-pragmas", "--no-bulk-pragmas"])
def test_backfill_cli_accepts_bulk_pragma_flags_and_restores_global_state(
    tmp_path, monkeypatch, flag,
):
    from contextlib import contextmanager

    from typer.testing import CliRunner

    from dbrel import db, cli
    from dbrel.ingest.replay import ReplayResult

    db_path = tmp_path / "isolated-cli.sqlite"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "TT_SWEEP_LOCK", tmp_path / "tt_sweep.lock")
    db.dispose_engine()
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Station(
            natural_key="bulk-cli-station", name="Bulk CLI station", lat=0, lon=0,
            eva=8000001, tracked=1,
        ))
        current.commit()

    @contextmanager
    def isolated_session_scope():
        with Session(engine, future=True) as current:
            yield current

    monkeypatch.setattr(cli, "session_scope", isolated_session_scope)
    monkeypatch.setattr("dbrel.ingest.replay.replay_day", lambda *_args, **_kwargs: ReplayResult())
    monkeypatch.setattr(db, "_BULK_LOAD", False, raising=False)
    actual_enable = db.enable_bulk_load
    states = []

    def track_bulk_state(enabled=True):
        states.append(enabled)
        return actual_enable(enabled)

    monkeypatch.setattr(db, "enable_bulk_load", track_bulk_state)
    root = tmp_path / "archive"
    root.mkdir()
    result = CliRunner().invoke(cli.app, [
        "backfill", "--from", "2026-05-10", "--to", "2026-05-10",
        "--root", str(root), flag,
    ])

    assert result.exit_code == 0, result.output
    assert db._BULK_LOAD is False
    assert states[-1] is False
    assert (True in states) is (flag == "--bulk-pragmas")
    engine.dispose()
    db.dispose_engine()


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
