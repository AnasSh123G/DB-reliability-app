"""notice_rate_daily: the per-day notice counts that notice-rate inputs are summed from."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.derive.notice_rates import (
    build_notice_rates, notice_kind, notices_per_1000_trips, sparse_months,
)
from dbrel.models import NoticeRateDaily, ServiceLine, Station, Train, Trip, TripStop, TtMessage

D1, D2 = "2026-09-15", "2026-09-16"
LINE = 1


def _stop(ts_id, trip_id, station_id, *, quality="RECORDED", arr_cancelled=0):
    return TripStop(id=ts_id, trip_id=trip_id, stop_sequence=ts_id, station_id=station_id,
                    supposed_arrival_utc=1_000_000 + ts_id, arr_delay_s=60, quality=quality,
                    rule_version=1, is_holiday=0, arr_cancelled=arr_cancelled)


def _message(trip_id, msg_id, category, *, ts=1, deleted=0, msg_type="h"):
    return TtMessage(trip_id=trip_id, stop_sequence=1, msg_id=msg_id, msg_type=msg_type,
                     category=category, ts=ts, deleted=deleted)


@pytest.fixture
def engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(ServiceLine(id=LINE, identity_key="RE/1", category="RE", line="RE1",
                          display_name="RE1", search_name="re1"))
        s.add(Station(id=1, natural_key="a", name="A", lat=1, lon=1, state="DE-NW"))
        s.add(Station(id=2, natural_key="b", name="B", lat=2, lon=2, state="DE-NW"))
        s.add(Train(id=10, identity_key="tr10", line_id=LINE))
        s.add(Train(id=11, identity_key="tr11", line_id=LINE))
        s.add(Train(id=12, identity_key="tr12", line_id=LINE))
        s.add(Train(id=20, identity_key="tr20"))  # no line
        # one trip per train per date
        for trip_id, train_id, date in [(1, 10, D1), (2, 11, D1), (3, 20, D1), (4, 12, D1), (5, 10, D2)]:
            s.add(Trip(id=trip_id, train_id=train_id, service_date=date, source_run_key=f"r{trip_id}"))
        s.add(_stop(1, 1, 1))
        s.add(_stop(2, 1, 2))
        s.add(_stop(3, 2, 1))
        s.add(_stop(4, 3, 1))
        s.add(_stop(5, 4, 1, arr_cancelled=1))  # not an eligible arrival
        s.add(_stop(6, 5, 1))
        s.flush()  # messages reference trips by plain foreign key, which the unit of work doesn't order
        # trip 1: a construction notice that was later withdrawn (still counts once), and a disruption
        s.add(_message(1, "a", "Bauarbeiten", ts=1))
        s.add(_message(1, "a", "Bauarbeiten. (Quelle: zuginfo.nrw)", ts=2, deleted=1))
        s.add(_message(1, "b", "Großstörung", ts=1))
        s.add(_message(1, "i", "Information", ts=1))  # not a notice kind
        s.add(_message(1, "r", "Störung", ts=1, msg_type="r"))  # not an 'h' message
        # trip 2: its only construction notice is deleted, so it has no notice
        s.add(_message(2, "c", "Bauarbeiten", ts=1, deleted=1))
        # trip 4's arrival is ineligible, so its notice is counted nowhere
        s.add(_message(4, "d", "Bauarbeiten", ts=1))
        s.commit()
    return engine


def _table(engine, date=None):
    with Session(engine, future=True) as s:
        query = select(NoticeRateDaily)
        if date:
            query = query.where(NoticeRateDaily.service_date == date)
        rows = s.execute(query).scalars().all()
    return {(r.level, r.key_id, r.service_date): (r.n, r.k_works, r.k_disruption) for r in rows}


def test_table_exists_in_a_fresh_database(engine):
    assert "notice_rate_daily" in inspect(engine).get_table_names()


def test_kinds_follow_the_category_prefix():
    assert notice_kind("Bauarbeiten") == "works"
    assert notice_kind("Bauarbeiten. (Quelle: zuginfo.nrw)") == "works"
    assert notice_kind("Störung") == "disruption"
    assert notice_kind("Großstörung") == "disruption"
    assert notice_kind("Information") is None
    assert notice_kind(None) is None


def test_counts_per_level(engine):
    build_notice_rates(engine)
    table = _table(engine, D1)
    assert table[("station", 1, D1)] == (3, 1, 1)   # trips 1 (with both notices), 2 and 3
    assert table[("station", 2, D1)] == (1, 1, 1)
    assert table[("line", LINE, D1)] == (3, 2, 2)   # trip 1 twice, trip 2 once; train 20 has no line
    assert table[("train", 10, D1)] == (2, 2, 2)    # trip 1's two stops
    assert table[("train", 11, D1)] == (1, 0, 0)    # trip 2: its only notice is deleted
    assert table[("train", 20, D1)] == (1, 0, 0)


def test_a_train_without_a_line_has_no_line_row(engine):
    build_notice_rates(engine)
    assert {k for (lvl, k, _) in _table(engine) if lvl == "line"} == {LINE}


def test_a_deleted_only_notice_and_an_ineligible_arrival_count_nowhere(engine):
    build_notice_rates(engine)
    table = _table(engine, D1)
    # trip 4 (cancelled arrival) adds nothing, and trip 2's deleted notice sets no flag:
    assert table[("station", 1, D1)][0] == 3
    assert ("train", 12, D1) not in table
    assert table[("train", 11, D1)] == (1, 0, 0)


def test_rebuilding_changes_nothing(engine):
    build_notice_rates(engine)
    first = _table(engine)
    build_notice_rates(engine)
    assert _table(engine) == first


def test_rebuilding_a_range_replaces_only_those_dates(engine):
    build_notice_rates(engine)
    with Session(engine, future=True) as s:
        s.add(_message(5, "e", "Störung", ts=1))
        s.commit()
    build_notice_rates(engine, first=D2, last=D2)
    assert _table(engine, D2)[("station", 1, D2)] == (1, 0, 1)
    assert _table(engine, D1)[("station", 1, D1)] == (3, 1, 1)


def test_month_totals_and_progress(engine):
    seen = []
    summary = build_notice_rates(engine, progress=lambda d, n: seen.append(d))
    assert seen == [D1, D2]
    assert summary["2026-09"] == {"trips": 4, "arrivals": 5, "works_notices": 1, "disruption_notices": 1}


def test_a_month_far_below_the_median_density_is_flagged():
    def month(density):
        return {"trips": 1000, "arrivals": 9000, "works_notices": density, "disruption_notices": 0}
    summary = {"2026-01": month(100), "2026-02": month(110), "2026-03": month(105), "2026-04": month(20)}
    assert sparse_months(summary) == ["2026-04"]
    assert notices_per_1000_trips(summary["2026-01"]) == 100
    assert sparse_months({"2026-01": month(100), "2026-02": month(1)}) == []  # too few months to judge


def test_migrating_an_older_database_adds_only_the_table(engine):
    from sqlalchemy import text

    from dbrel.schema_migrate import migrate

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE notice_rate_daily"))
    before = _count(engine, "trip_stop"), _count(engine, "tt_message")
    first = migrate(engine)
    assert "notice_rate_daily" in inspect(engine).get_table_names()
    assert (_count(engine, "trip_stop"), _count(engine, "tt_message")) == before
    assert not any(first.values())  # nothing it reports on had to change
    build_notice_rates(engine)
    snapshot = _table(engine)
    assert not any(migrate(engine).values())
    assert _table(engine) == snapshot  # a second migration leaves the data alone


def _count(engine, table):
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _run_cli(monkeypatch, db_path, args):
    from typer.testing import CliRunner

    import dbrel.config as config
    import dbrel.db as dbmod
    from dbrel.cli import app

    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    dbmod.dispose_engine()
    try:
        return CliRunner().invoke(app, ["ml", "build-notice-rates", *args])
    finally:
        dbmod.dispose_engine()


def test_cli_lists_the_months_and_fills_the_table(tmp_path, monkeypatch):
    import sqlite3

    from ml_fixture import build

    path = tmp_path / "db.sqlite"
    build(path)
    result = _run_cli(monkeypatch, path, [])
    assert result.exit_code == 0, result.output
    assert "2026-09" in result.output and "works/1k trips" in result.output
    assert "WARNING" not in result.output  # one month is too few to judge
    (rows,) = sqlite3.connect(path).execute("SELECT COUNT(*) FROM notice_rate_daily").fetchone()
    assert rows > 0


def test_cli_flags_a_sparse_month(tmp_path, monkeypatch):
    import dbrel.derive.notice_rates as module
    from ml_fixture import build

    def month(density):
        return {"trips": 1000, "arrivals": 9000, "works_notices": density, "disruption_notices": 0}

    monkeypatch.setattr(module, "build_notice_rates", lambda *a, **k: {
        "2026-01": month(100), "2026-02": month(110), "2026-03": month(105), "2026-04": month(20)})
    path = tmp_path / "db.sqlite"
    build(path)
    result = _run_cli(monkeypatch, path, [])
    assert result.exit_code == 0, result.output
    assert "2026-04" in result.output and "far fewer notices" in result.output
    assert "WARNING: 1 month(s)" in result.output


def test_cli_limits_a_rebuild_to_the_given_dates(tmp_path, monkeypatch):
    import sqlite3

    from ml_fixture import DATES, build

    path = tmp_path / "db.sqlite"
    build(path)
    first, last = DATES[3], DATES[4]
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM notice_rate_daily")  # the fixture builds the whole table; start clean
    conn.commit()
    conn.close()
    result = _run_cli(monkeypatch, path, ["--from", first, "--to", last])
    assert result.exit_code == 0, result.output
    dates = {d for (d,) in sqlite3.connect(path).execute("SELECT DISTINCT service_date FROM notice_rate_daily")}
    assert dates == {first, last}


def test_the_builder_creates_its_table_in_a_database_that_predates_it(engine):
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE notice_rate_daily"))
    assert "notice_rate_daily" not in inspect(engine).get_table_names()
    build_notice_rates(engine)
    assert "notice_rate_daily" in inspect(engine).get_table_names() and _table(engine)
