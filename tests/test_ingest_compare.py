"""Behavioral checks for the read-only natural-key comparison command."""
from contextlib import closing
import shutil
import sqlite3

import pytest

from dbrel.ingest_compare import compare_databases, main


@pytest.fixture
def pair(tmp_path):
    a, b = tmp_path / 'a.sqlite', tmp_path / 'b.sqlite'
    with sqlite3.connect(a) as db:
        db.executescript('''
        CREATE TABLE feed_version(id INTEGER PRIMARY KEY, kind TEXT, source_url TEXT, imported_at INT, content_sha256 TEXT);
        CREATE TABLE station(id INTEGER PRIMARY KEY, name TEXT, first_seen_feed_version_id INT);
        CREATE TABLE service_line(id INTEGER PRIMARY KEY, identity_key TEXT);
        CREATE TABLE train(id INTEGER PRIMARY KEY, identity_key TEXT, line_id INT, first_seen_feed_version_id INT, n_runs_seen INT, n_stops_tracked INT);
        CREATE TABLE search_alias(id INTEGER PRIMARY KEY, alias_norm TEXT, kind TEXT, train_id INT, line_id INT, station_id INT, weight INT);
        CREATE TABLE station_stop(id INTEGER PRIMARY KEY, feed_version_id INT, train_id INT, path_sha1 TEXT, stop_sequence INT);
        CREATE TABLE trip(id INTEGER PRIMARY KEY, source TEXT, source_run_key TEXT, service_date TEXT, train_id INT, feed_version_id INT, finalized_at INT);
        CREATE TABLE trip_stop(id INTEGER PRIMARY KEY, trip_id INT, stop_sequence INT, station_stop_id INT, dep_delay_s INT);
        CREATE TABLE trip_schedule_snapshot(id INTEGER PRIMARY KEY, trip_id INT, stop_sequence INT, observed_at INT, source_content_sha256 TEXT, value_fingerprint TEXT, station_stop_id INT);
        CREATE TABLE tt_message(id INTEGER PRIMARY KEY, trip_id INT, stop_sequence INT, msg_id TEXT, ts INT, code INT);
        CREATE TABLE tt_poll(id INTEGER PRIMARY KEY, sweep_id INT, eva INT, fetched_at INT, fetch_ms INT, parse_ms INT, store_ms INT);
        CREATE TABLE tt_plan_fetch(id INTEGER PRIMARY KEY, eva INT, plan_date TEXT, plan_hour INT, n_stops INT);
        CREATE TABLE tt_unmatched(id INTEGER PRIMARY KEY, service_date TEXT, daily_trip_id TEXT, stop_index INT, first_poll_id INT, last_poll_id INT);
        CREATE TABLE aggregate_ignored(value INT);
        INSERT INTO feed_version VALUES(1,'tt','url',5,'hash');
        INSERT INTO station VALUES(1,'Berlin',1);
        INSERT INTO service_line VALUES(1,'RE/1');
        INSERT INTO train VALUES(1,'RE/123',1,1,1,1),(2,'RE/456',1,1,1,1);
        INSERT INTO search_alias VALUES(1,'re123','train',1,NULL,NULL,100),(2,'re456','train',2,NULL,NULL,100),(3,'re1','line',NULL,1,NULL,100),(4,'berlin','station',NULL,NULL,1,100);
        INSERT INTO station_stop VALUES(1,1,1,'path',1),(2,1,2,'path',1);
        INSERT INTO trip VALUES(1,'tt','run1','2026-03-29',1,1,NULL),(2,'tt','run2','2026-01-01',2,1,NULL);
        INSERT INTO trip_stop VALUES(1,1,1,1,60),(2,2,1,2,60);
        INSERT INTO trip_schedule_snapshot VALUES(1,1,1,10,'hash','value',1);
        INSERT INTO tt_message VALUES(1,1,1,NULL,NULL,1),(2,1,1,NULL,NULL,1);
        INSERT INTO tt_poll VALUES(1,1,8001,1774738800,1,2,3),(2,2,8001,0,1,2,3);
        INSERT INTO tt_plan_fetch VALUES(1,8001,'2026-03-29',1,1),(2,8001,'2026-01-01',1,1);
        INSERT INTO tt_unmatched VALUES(1,'2026-03-29','daily',1,1,1);
        ''')
    shutil.copyfile(a, b)
    return a, b


def change(path, sql):
    with closing(sqlite3.connect(path)) as db:
        db.executescript(sql)


def test_identical_and_timing_ignored(pair):
    a,b = pair
    change(b, 'UPDATE tt_poll SET fetch_ms=999, parse_ms=999, store_ms=999')
    result = compare_databases(a,b)
    assert result['identical']
    assert 'aggregate_ignored' in result['not_compared']
    assert main([str(a),str(b)]) == 0


def test_delay_difference_names_key_and_column(pair, capsys):
    a,b = pair
    change(b, 'UPDATE trip_stop SET dep_delay_s=120 WHERE id=1')
    assert main([str(a),str(b)]) == 1
    output = capsys.readouterr().out
    assert all(s in output for s in ('trip_stop','run1','dep_delay_s'))
    report=compare_databases(a,b)['tables']['trip_stop']
    assert report['differing'] == 1
    assert report['only_a'] == report['only_b'] == 0


def test_surrogate_ids_and_all_foreign_keys_are_normalized(pair):
    a,b=pair
    change(b, '''UPDATE feed_version SET id=id+10;
    UPDATE station SET first_seen_feed_version_id=11;
    UPDATE service_line SET id=id+10;
    UPDATE train SET id=id+10,line_id=11,first_seen_feed_version_id=11;
    UPDATE station_stop SET id=id+10,feed_version_id=11,train_id=train_id+10;
    UPDATE trip SET id=id+10,train_id=train_id+10,feed_version_id=11;
    UPDATE trip_stop SET id=id+10,trip_id=trip_id+10,station_stop_id=station_stop_id+10;
    UPDATE trip_schedule_snapshot SET id=id+10,trip_id=trip_id+10,station_stop_id=station_stop_id+10;
    UPDATE tt_message SET id=id+10,trip_id=trip_id+10;
    UPDATE search_alias SET id=id+10,train_id=train_id+10,line_id=line_id+10;
    UPDATE tt_poll SET id=id+10;
    UPDATE tt_unmatched SET first_poll_id=11,last_poll_id=11;
    ''')
    assert compare_databases(a,b)['identical']


def test_allow_only_hides_named_column(pair):
    a,b=pair
    change(b, 'UPDATE train SET n_runs_seen=2')
    assert not compare_databases(a,b)['identical']
    assert compare_databases(a,b,allow=['train.n_runs_seen'])['identical']
    change(b, 'UPDATE train SET n_stops_tracked=2')
    assert not compare_databases(a,b,allow=['train.n_runs_seen'])['identical']


