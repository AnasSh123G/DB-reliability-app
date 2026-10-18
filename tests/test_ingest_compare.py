"""Behavioral checks for the read-only natural-key comparison command."""
import json
from contextlib import closing
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

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


def test_deleted_snapshot_and_duplicate_multisets(pair):
    a,b=pair
    change(b, 'DELETE FROM trip_schedule_snapshot; DELETE FROM tt_message WHERE id=1')
    report=compare_databases(a,b)['tables']
    assert report['trip_schedule_snapshot']['only_a'] == 1
    assert report['tt_message']['only_a'] == 1


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


def test_dates_scope_and_local_midnight_padding(pair):
    a,b=pair
    change(b, '''UPDATE trip_stop SET dep_delay_s=999 WHERE trip_id=2;
    UPDATE train SET n_runs_seen=999 WHERE id=2;
    UPDATE station_stop SET path_sha1='other' WHERE train_id=2;
    UPDATE search_alias SET weight=999 WHERE train_id=2;
    UPDATE tt_plan_fetch SET n_stops=999 WHERE id=2;
    UPDATE tt_poll SET eva=999 WHERE id=2;
    ''')
    assert compare_databases(a,b,dates=['2026-03-29'])['identical']
    # 2026-03-28 midnight Berlin is 1774652400, inside padding.
    change(b, 'INSERT INTO tt_poll VALUES(3,3,8001,1774652400,0,0,0)')
    assert compare_databases(a,b,dates=['2026-03-29'])['tables']['tt_poll']['only_b']==1


def test_finalized_changes_include_outside_date_and_dependents(pair):
    a,b=pair
    change(b, 'UPDATE trip SET finalized_at=99 WHERE id=2; UPDATE trip_stop SET dep_delay_s=999 WHERE trip_id=2')
    report=compare_databases(a,b,dates=['2026-03-29'])
    assert not report['identical']
    assert report['tables']['trip']['differing']==1
    assert report['tables']['trip_stop']['differing']==1


def test_missing_tables_schema_new_columns_and_samples(pair):
    a,b=pair
    change(b, 'DROP TABLE tt_message; ALTER TABLE trip_stop ADD COLUMN new_evidence TEXT')
    report=compare_databases(a,b,samples=0)
    assert not report['identical']
    assert report['tables']['tt_message']['missing']=='b'
    assert report['tables']['trip_stop']['schema_only_b']==['new_evidence']
    assert report['tables']['trip_stop']['samples']==[]


def test_cli_json_usage_and_no_creation(pair,tmp_path):
    a,b=pair
    out=tmp_path/'report.json'
    proc=subprocess.run([sys.executable,'scripts/compare_ingest_dbs.py',str(a),str(b),'--json',str(out)],capture_output=True,text=True)
    assert proc.returncode==0,proc.stderr
    assert json.loads(out.read_text())['identical']
    assert main([str(a),str(b),'--allow','train.typo'])==2
    assert main([str(a),str(b),'--dates','bad'])==2
    missing=tmp_path/'missing.sqlite'
    assert main([str(a),str(missing)])==2
    assert not missing.exists()


def test_poll_window_has_dst_aware_exclusive_upper_midnight(pair):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    a,b=pair
    upper=int(datetime(2026,3,31,tzinfo=ZoneInfo('Europe/Berlin')).timestamp())
    change(b, f'INSERT INTO tt_poll VALUES(3,3,8001,{upper},0,0,0)')
    assert compare_databases(a,b,dates=['2026-03-29'])['identical']
    change(b, f'UPDATE tt_poll SET fetched_at={upper-1} WHERE id=3')
    assert not compare_databases(a,b,dates=['2026-03-29'])['identical']


def test_multiset_changes_pair_rows_and_keep_excess(pair):
    a,b=pair
    change(b, '''UPDATE tt_message SET code=2;
    INSERT INTO tt_message VALUES(3,1,1,NULL,NULL,2);
    ''')
    report=compare_databases(a,b)['tables']['tt_message']
    assert report['rows_a']==2
    assert report['rows_b']==3
    assert report['differing']==2
    assert report['only_a']==0
    assert report['only_b']==1


def test_inputs_remain_byte_identical_after_date_comparison(pair):
    a,b=pair
    before=[p.read_bytes() for p in pair]
    assert compare_databases(a,b,dates=['2026-03-29'])['identical']
    assert [p.read_bytes() for p in pair]==before
    assert main([str(a),str(b),'--json',str(a)])==2
    assert [p.read_bytes() for p in pair]==before
    assert main([str(a),str(b),'--samples','-1'])==2


def test_all_current_model_columns_can_be_compared(tmp_path):
    from sqlalchemy import create_engine
    from dbrel.models import Base
    a,b=tmp_path/'full-a.sqlite',tmp_path/'full-b.sqlite'
    engine=create_engine(f'sqlite:///{a}')
    Base.metadata.create_all(engine)
    engine.dispose()
    shutil.copyfile(a,b)
    assert compare_databases(a,b)['identical']
    assert compare_databases(a,b,dates=['2026-03-29'])['identical']


def test_full_schema_distinct_feed_contents_do_not_hide_fk_repoints(tmp_path):
    from sqlalchemy import create_engine
    from dbrel.models import Base
    a,b=tmp_path/'versions-a.sqlite',tmp_path/'versions-b.sqlite'
    engine=create_engine(f'sqlite:///{a}')
    Base.metadata.create_all(engine)
    engine.dispose()
    change(a, '''
        INSERT INTO feed_version(id,kind,source_url,content_sha256,downloaded_at,status)
        VALUES(1,'rv','https://same-url','hash-a',1,'ready'),
              (2,'rv','https://same-url','hash-b',2,'ready');
        INSERT INTO station(id,natural_key,name,lat,lon,tracked,poll_priority,
                            n_fchg_polls,n_fchg_errors,first_seen_feed_version_id)
        VALUES(1,'berlin','Berlin',52,13,1,1,0,0,1);
    ''')
    shutil.copyfile(a,b)
    change(b, 'UPDATE station SET first_seen_feed_version_id=2')
    report=compare_databases(a,b)
    assert not report['identical']
    assert report['tables']['station']['differing']==1
    assert 'first_seen_feed_version_id' in report['tables']['station']['samples'][0]['columns']


def test_json_hardlink_cannot_overwrite_either_database(pair,tmp_path):
    import os
    for index, source in enumerate(pair):
        alias=tmp_path/f'input-alias-{index}.json'
        os.link(source,alias)
        before=[p.read_bytes() for p in pair]
        assert main([str(pair[0]),str(pair[1]),'--json',str(alias)])==2
        assert [p.read_bytes() for p in pair]==before


def test_missing_parent_is_structured_difference_not_join_error(pair,capsys):
    a,b=pair
    change(b,'DROP TABLE service_line')
    report=compare_databases(a,b)
    assert not report['identical']
    assert report['tables']['service_line']['missing']=='b'
    assert report['tables']['train']['rows_a']==2
    assert report['tables']['train']['rows_b']==2
    assert report['tables']['train']['error']
    assert main([str(a),str(b)])==1
    assert 'service_line' in capsys.readouterr().out


def test_missing_key_column_is_reported_without_partial_certification(pair):
    a,b=pair
    change(b,'ALTER TABLE service_line DROP COLUMN identity_key')
    report=compare_databases(a,b)
    assert not report['identical']
    assert report['tables']['service_line']['schema_only_a']==['identity_key']
    assert report['tables']['train']['compared'] is False
    assert report['tables']['train']['differing'] is None


def test_missing_trip_with_dates_reports_scope_failure(pair):
    a,b=pair
    change(b,'DROP TABLE trip')
    report=compare_databases(a,b,dates=['2026-03-29'])
    assert not report['identical']
    assert report['scope_error']
    assert report['tables']['trip']['missing']=='b'
    assert report['tables']['trip_stop']['compared'] is False
    assert main([str(a),str(b),'--dates','2026-03-29'])==1


@pytest.mark.parametrize('sidecar', ['wal', 'shm'])
@pytest.mark.parametrize('alias_kind', ['literal', 'symlink', 'hardlink'])
@pytest.mark.parametrize('input_symlink', [False, True])
def test_json_cannot_target_input_sqlite_sidecar(
    pair, tmp_path, sidecar, alias_kind, input_symlink, monkeypatch
):
    import os

    a, b = pair
    writer = sqlite3.connect(a)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('PRAGMA wal_autocheckpoint=0')
    writer.execute('CREATE TABLE wal_probe(value TEXT)')
    writer.execute("INSERT INTO wal_probe VALUES ('uncheckpointed')")
    writer.commit()

    sidecar_path = Path(f'{a}-{sidecar}')
    assert sidecar_path.exists(), f'{sidecar} must exist while the WAL writer stays open'
    source_arg = a
    if input_symlink:
        source_arg = tmp_path / 'input-symlink.sqlite'
        source_arg.symlink_to(a)

    if alias_kind == 'literal':
        json_path = sidecar_path
    else:
        json_path = tmp_path / f'{sidecar}-{alias_kind}-alias'
        if alias_kind == 'symlink':
            json_path.symlink_to(sidecar_path)
        else:
            os.link(sidecar_path, json_path)

    before_sidecar = sidecar_path.read_bytes()
    before_database = a.read_bytes()
    attempted_sidecar_writes = []
    comparisons = []
    original_write_text = Path.write_text
    from dbrel import ingest_compare as comparator
    original_compare = comparator.compare_databases

    def tracked_compare(*args, **kwargs):
        comparisons.append(True)
        return original_compare(*args, **kwargs)

    def guarded_write_text(path, data, *args, **kwargs):
        if (path.resolve() == sidecar_path.resolve()
                or (path.exists() and path.samefile(sidecar_path))):
            attempted_sidecar_writes.append(path)
            return len(data)
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', guarded_write_text)
    monkeypatch.setattr(comparator, 'compare_databases', tracked_compare)
    try:
        result = main([str(source_arg), str(b), '--json', str(json_path)])

        after_sidecar = sidecar_path.read_bytes()
        after_database = a.read_bytes()
        if alias_kind == 'symlink':
            assert json_path.is_symlink()
            assert json_path.resolve() == sidecar_path.resolve()
        assert not comparisons, 'SQLite sidecar destinations must be rejected before comparison'
        assert result == 2, 'SQLite sidecar destinations must be rejected before comparison'
        assert not attempted_sidecar_writes, 'rejection must happen before output is written'
        assert after_sidecar == before_sidecar
        assert after_database == before_database
        with closing(sqlite3.connect(source_arg)) as reader:
            fresh_value = reader.execute('SELECT value FROM wal_probe').fetchone()
        assert fresh_value == ('uncheckpointed',)
    finally:
        writer.close()
