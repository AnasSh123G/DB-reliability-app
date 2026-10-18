"""Migration contract for adding the append-only dated-plan evidence table."""
from __future__ import annotations

import importlib

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    FeedVersion, Station, StationStop, Train, Trip, TripStop,
)

TABLE = "trip_schedule_snapshot"


def _ensure(engine):
    """Resolve lazily so the missing migration fails each contract, not collection."""
    module = importlib.import_module("dbrel.ingest.schedule_schema")
    return module.ensure_dated_schedule_schema(engine)


def _collection_schema(engine):
    tables = [table for table in Base.metadata.sorted_tables if table.name != TABLE]
    Base.metadata.create_all(engine, tables=tables)


def test_schema_migration_creates_only_new_table_once_and_preserves_facts():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _collection_schema(engine)
    before = set(inspect(engine).get_table_names())
    assert TABLE not in before
    with Session(engine) as session:
        feed = FeedVersion(kind="rv", source_url="fixture", content_sha256="a" * 64, downloaded_at=1)
        station = Station(natural_key="s@1,1", name="Stop", lat=1, lon=1)
        train = Train(source=SOURCE_TT, identity_kind="NUMBERED", identity_key="tts:RE/5")
        session.add_all([feed, station, train]); session.flush()
        station_stop = StationStop(feed_version_id=feed.id, train_id=train.id, station_id=station.id,
                                   stop_sequence=4, path_sha1="b" * 40,
                                   planned_arrival_sec=100, planned_departure_sec=110)
        trip = Trip(train_id=train.id, service_date="2026-09-01", source=SOURCE_TT,
                    source_run_key="run-1", path_sha1="b" * 40)
        session.add_all([station_stop, trip]); session.flush()
        fact = TripStop(trip_id=trip.id, stop_sequence=4, station_id=station.id,
                        station_stop_id=station_stop.id, supposed_arrival_utc=1000,
                        actual_arrival_utc=1010, arr_delay_s=10, quality="RECORDED",
                        rule_version=3, last_observed_at=1020)
        session.add(fact); session.commit()
        facts_before = session.execute(select(
            TripStop.id, TripStop.supposed_arrival_utc, TripStop.actual_arrival_utc,
            TripStop.arr_delay_s, TripStop.quality, TripStop.rule_version,
        )).all()

        assert _ensure(engine) is True
        after_first = set(inspect(engine).get_table_names())
        assert after_first == before | {TABLE}
        assert _ensure(engine) is False
        assert set(inspect(engine).get_table_names()) == after_first
        assert session.execute(select(
            TripStop.id, TripStop.supposed_arrival_utc, TripStop.actual_arrival_utc,
            TripStop.arr_delay_s, TripStop.quality, TripStop.rule_version,
        )).all() == facts_before
        assert inspect(engine).get_columns(TABLE)
        assert inspect(engine).get_foreign_keys(TABLE)
    engine.dispose()


def test_schema_migration_rejects_database_without_collection_parent_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with pytest.raises(ValueError, match="(?i)(collection|parent|trip|station)"):
        _ensure(engine)
    assert TABLE not in inspect(engine).get_table_names()
    engine.dispose()
