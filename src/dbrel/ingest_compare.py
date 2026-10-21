"""Read-only, streaming multiset comparison of archive ingest databases.

Feed versions include their content hash because a source URL is reused across
GTFS releases and cannot distinguish foreign-key re-points.

SQLite orders normalized natural keys on disk; Python retains only one key's
row Counter and the requested examples, never an entire fact table. SQLite's
sort cache is bounded and temporary storage is file-backed for large databases.
Counts are row multiplicities: matching-key changes consume one row on each
side as ``differing``; excess rows are ``only_a`` or ``only_b``.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, closing
from datetime import date, datetime, time, timedelta
from itertools import groupby
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from zoneinfo import ZoneInfo


KEYS = {
    'feed_version': ('kind', 'source_url', 'content_sha256'),
    'station': ('id',),
    'service_line': ('identity_key',),
    'train': ('identity_key',),
    'search_alias': ('alias_norm', 'kind', 'target'),
    'station_stop': ('feed_version_id', 'train_id', 'path_sha1', 'stop_sequence'),
    'trip': ('source', 'source_run_key', 'service_date'),
    'trip_stop': ('trip_id', 'stop_sequence'),
    'trip_schedule_snapshot': ('trip_id', 'stop_sequence', 'observed_at',
                               'source_content_sha256', 'value_fingerprint'),
    'tt_message': ('trip_id', 'stop_sequence', 'msg_id', 'ts'),
    'tt_poll': ('sweep_id', 'eva'),
    'tt_plan_fetch': ('eva', 'plan_date', 'plan_hour'),
    'tt_unmatched': ('service_date', 'daily_trip_id', 'stop_index'),
}
TIMINGS = {'fetch_ms', 'parse_ms', 'store_ms'}
TRIP_CHILDREN = {'trip_stop', 'trip_schedule_snapshot', 'tt_message'}


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _uri(path: Path) -> str:
    return path.resolve().as_uri() + '?mode=ro'


def _open(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(_uri(path), uri=True)
    db.execute('PRAGMA temp_store=FILE')
    db.execute('PRAGMA cache_size=-32768')
    return db


def _tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f'PRAGMA table_info({_quote(table)})')]


def _reference(column: str) -> str | None:
    if column.endswith('feed_version_id'):
        return 'feed_version'
    return {'train_id': 'train', 'line_id': 'service_line', 'trip_id': 'trip',
            'station_stop_id': 'station_stop', 'first_poll_id': 'tt_poll',
            'last_poll_id': 'tt_poll'}.get(column)


class _Query:
    """Expand FK natural keys with joins, sharing each join within a query."""

    def __init__(self, table: str):
        self.table = table
        self.joins: dict[tuple[str, str], str] = {}
        self.join_sql: list[str] = []

    def value(self, alias: str, column: str) -> str:
        raw = f'{alias}.{_quote(column)}'
        target = _reference(column)
        if target is None:
            return raw
        pair = (alias, column)
        if pair not in self.joins:
            joined = f'j{len(self.joins)}'
            self.joins[pair] = joined
            self.join_sql.append(f'LEFT JOIN {_quote(target)} {joined} ON {joined}.id={raw}')
        key = self.key(target, self.joins[pair])
        # Preserve dangling references instead of collapsing every orphan to NULL.
        return f"CASE WHEN {raw} IS NULL THEN NULL WHEN {self.joins[pair]}.id IS NULL THEN json_array('missing-reference',{raw}) ELSE {key} END"

    def key(self, table: str, alias: str = 'r') -> str:
        if table == 'search_alias':
            train = self.value(alias, 'train_id')
            line = self.value(alias, 'line_id')
            target = (f"CASE WHEN {alias}.train_id IS NOT NULL THEN json_array('T',json({train})) "
                      f"WHEN {alias}.line_id IS NOT NULL THEN json_array('L',json({line})) "
                      f"ELSE json_array('S',{alias}.station_id) END")
            return f'json_array({alias}.alias_norm,{alias}.kind,json({target}))'
        values = [self.value(alias, c) for c in KEYS[table]]
        values = [f'json({v})' if _reference(c) else v for c, v in zip(KEYS[table], values)]
        return 'json_array(' + ','.join(values) + ')'


def _selected_sql(alias: str) -> str:
    return (f'EXISTS (SELECT 1 FROM temp.selected_trip k WHERE k.source={alias}.source '
            f'AND k.source_run_key={alias}.source_run_key AND k.service_date={alias}.service_date)')


def _prepare_dates(a: sqlite3.Connection, b: sqlite3.Connection,
                   b_path: Path, dates: tuple[str, ...]) -> None:
    """Persist the union of window and changed-finalization trip keys in temp DBs."""
    definition = ('CREATE TEMP TABLE selected_trip(source TEXT, source_run_key TEXT, '
                  'service_date TEXT, PRIMARY KEY(source,source_run_key,service_date)) WITHOUT ROWID')
    a.execute(definition)
    b.execute(definition)
    a.execute('ATTACH DATABASE ? AS other', (_uri(b_path),))
    marks = ','.join('?' for _ in dates)
    for source, other in [('main', 'other'), ('other', 'main')]:
        # IS compares NULL timestamps correctly; missing trips are included too.
        sql = (f'INSERT OR IGNORE INTO temp.selected_trip '
               f'SELECT t.source,t.source_run_key,t.service_date FROM {source}.trip t '
               f'WHERE t.service_date IN ({marks}) OR NOT EXISTS '
               f'(SELECT 1 FROM {other}.trip o WHERE o.source=t.source '
               'AND o.source_run_key=t.source_run_key AND o.service_date=t.service_date '
               'AND o.finalized_at IS t.finalized_at)')
        a.execute(sql, dates)
    cursor = a.execute('SELECT source,source_run_key,service_date FROM temp.selected_trip')
    while rows := cursor.fetchmany(10_000):
        b.executemany('INSERT INTO temp.selected_trip VALUES (?,?,?)', rows)


def _scope(table: str, dates: tuple[str, ...]) -> tuple[str, tuple]:
    if not dates:
        return '', ()
    if table == 'trip':
        return 'WHERE ' + _selected_sql('r'), ()
    if table in TRIP_CHILDREN:
        return 'WHERE EXISTS (SELECT 1 FROM trip t WHERE t.id=r.trip_id AND ' + _selected_sql('t') + ')', ()
    train_scope = ('SELECT t.train_id FROM trip t WHERE ' + _selected_sql('t'))
    if table == 'train':
        return f'WHERE r.id IN ({train_scope})', ()
    if table == 'station_stop':
        return f'WHERE r.train_id IN ({train_scope})', ()
    if table == 'search_alias':
        # Station aliases and aliases for the selected trains' lines remain relevant.
        return (f'WHERE r.train_id IN ({train_scope}) OR r.station_id IS NOT NULL '
                f'OR r.line_id IN (SELECT line_id FROM train WHERE id IN ({train_scope}))'), ()
    if table in {'tt_plan_fetch', 'tt_unmatched'}:
        col = 'plan_date' if table == 'tt_plan_fetch' else 'service_date'
        return f'WHERE r.{col} IN ({",".join("?" for _ in dates)})', dates
    if table == 'tt_poll':
        zone = ZoneInfo('Europe/Berlin')
        first = date.fromisoformat(min(dates)) - timedelta(days=1)
        last = date.fromisoformat(max(dates)) + timedelta(days=2)
        bounds = tuple(int(datetime.combine(d, time(), zone).timestamp()) for d in (first, last))
        return 'WHERE r.fetched_at>=? AND r.fetched_at<?', bounds
    return '', ()


def _query_sql(table: str, columns: list[str], dates: tuple[str, ...]) -> tuple[str, tuple]:
    query = _Query(table)
    key = query.key(table)
    values = [query.value('r', c) for c in columns]
    where, params = _scope(table, dates)
    sql = (f'SELECT {key} AS natural_key' + ''.join(f',{v}' for v in values)
           + f' FROM {_quote(table)} r ' + ' '.join(query.join_sql)
           + ' ' + where + ' ORDER BY natural_key COLLATE BINARY')
    return sql, params


def _groups(db: sqlite3.Connection, table: str, columns: list[str],
            dates: tuple[str, ...]) -> Iterator[tuple[str, Counter]]:
    sql, params = _query_sql(table, columns, dates)
    cursor = db.execute(sql, params)
    try:
        for natural_key, rows in groupby(cursor, key=lambda row: row[0]):
            yield natural_key, Counter(tuple(row[1:]) for row in rows)
    finally:
        cursor.close()


def _example(key: str, left: Counter, right: Counter, columns: list[str]) -> dict:
    a = next(iter(left), None)
    b = next(iter(right), None)
    differing = [c for i,c in enumerate(columns) if a is None or b is None or a[i] != b[i]]
    return {'key': json.loads(key), 'columns': differing,
            'a': dict(zip(columns,a)) if a is not None else None,
            'b': dict(zip(columns,b)) if b is not None else None}


def _compare_table(a, b, table, columns, dates, samples) -> dict:
    result = {'rows_a': 0, 'rows_b': 0, 'only_a': 0, 'only_b': 0,
              'differing': 0, 'samples': []}
    left, right = iter(_groups(a,table,columns,dates)), iter(_groups(b,table,columns,dates))
    ga, gb = next(left,None), next(right,None)
    while ga is not None or gb is not None:
        key = min(g[0] for g in (ga,gb) if g is not None)
        ca = ga[1] if ga and ga[0] == key else Counter()
        cb = gb[1] if gb and gb[0] == key else Counter()
        result['rows_a'] += ca.total()
        result['rows_b'] += cb.total()
        excess_a, excess_b = ca-cb, cb-ca
        paired = min(excess_a.total(),excess_b.total())
        result['differing'] += paired
        result['only_a'] += excess_a.total()-paired
        result['only_b'] += excess_b.total()-paired
        if (excess_a or excess_b) and len(result['samples']) < samples:
            result['samples'].append(_example(key,excess_a,excess_b,columns))
        if ga and ga[0] == key:
            ga = next(left,None)
        if gb and gb[0] == key:
            gb = next(right,None)
    return result


def _preflight(a, b, table, columns, dates) -> list[str]:
    """Compile both queries before opening streams, including recursive FK joins."""
    sql, params = _query_sql(table, columns, dates)
    errors = []
    for name, db in [('a', a), ('b', b)]:
        try:
            db.execute('EXPLAIN QUERY PLAN ' + sql, params).close()
        except sqlite3.Error as exc:
            errors.append(f'{name}: {exc}')
    return errors


def _incomplete_report(a, b, table, errors) -> dict:
    # Dependency failures may make date-scoped counting impossible. Expose totals
    # explicitly and never classify unexamined rows as equal or different.
    counts = [db.execute(f'SELECT count(*) FROM {_quote(table)}').fetchone()[0]
              for db in (a,b)]
    return {'rows_a': counts[0], 'rows_b': counts[1], 'row_counts_scope': 'all',
            'only_a': None, 'only_b': None, 'differing': None,
            'samples': [], 'compared': False, 'error': '; '.join(errors)}


def compare_databases(a: str | Path, b: str | Path, *, dates=(), allow=(), samples=5) -> dict:
    """Return a JSON-serializable report, opening both databases read-only.

    ``allow`` contains exact ``table.column`` names; unknown names are errors.
    New columns are discovered automatically and schema differences fail equality.
    """
    dates = tuple(sorted({date.fromisoformat(d).isoformat() for d in dates}))
    if samples < 0:
        raise ValueError('samples must be non-negative')
    allowed = set(allow)
    with ExitStack() as stack:
        da = stack.enter_context(closing(_open(Path(a))))
        db = stack.enter_context(closing(_open(Path(b))))
        tables_a, tables_b = _tables(da), _tables(db)
        schemas_a = {t: _columns(da,t) for t in KEYS if t in tables_a}
        schemas_b = {t: _columns(db,t) for t in KEYS if t in tables_b}
        valid = {f'{t}.{c}' for schemas in (schemas_a,schemas_b) for t,cs in schemas.items() for c in cs}
        if unknown := allowed-valid:
            raise ValueError('unknown allowed columns: ' + ', '.join(sorted(unknown)))
        scope_error = None
        if dates:
            if 'trip' not in tables_a & tables_b:
                scope_error = '--dates requires trip tables in both databases'
            else:
                try:
                    _prepare_dates(da,db,Path(b),dates)
                except sqlite3.Error as exc:
                    scope_error = f'cannot select date-scoped trips: {exc}'
        result = {'identical': scope_error is None, 'tables': {},
                  'not_compared': sorted((tables_a|tables_b)-KEYS.keys())}
        if scope_error:
            result['scope_error'] = scope_error
        for table in KEYS:
            if table not in tables_a | tables_b:
                continue
            if table not in tables_a & tables_b:
                present = da if table in tables_a else db
                count = present.execute(f'SELECT count(*) FROM {_quote(table)}').fetchone()[0]
                result['tables'][table] = {'missing': 'b' if table in tables_a else 'a',
                    'rows_a': count if table in tables_a else 0,
                    'rows_b': count if table in tables_b else 0,
                    'only_a': count if table in tables_a else 0,
                    'only_b': count if table in tables_b else 0, 'differing': 0, 'samples': []}
                result['identical'] = False
                continue
            drop = {'id'} if table != 'station' else set()
            if table == 'tt_poll':
                drop |= TIMINGS
            drop |= {name.split('.',1)[1] for name in allowed if name.startswith(table+'.')}
            ca,cb = set(schemas_a[table])-drop, set(schemas_b[table])-drop
            columns = sorted(ca&cb)
            errors = ([scope_error] if scope_error and _scope(table,dates)[0]
                      else _preflight(da,db,table,columns,dates))
            report = (_incomplete_report(da,db,table,errors) if errors
                      else _compare_table(da,db,table,columns,dates,samples))
            report['schema_only_a'],report['schema_only_b'] = sorted(ca-cb),sorted(cb-ca)
            if errors or ca != cb or any(report[c] for c in ('only_a','only_b','differing')):
                result['identical'] = False
            result['tables'][table] = report
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('a',type=Path)
    parser.add_argument('b',type=Path)
    parser.add_argument('--dates',default='',help='Comma-separated ISO service dates')
    parser.add_argument('--allow',default='',help='Comma-separated table.column names to ignore')
    parser.add_argument('--samples',type=int,default=5)
    parser.add_argument('--json',type=Path,dest='json_path')
    args = parser.parse_args(argv)
    try:
        if args.json_path:
            for source in (args.a,args.b):
                for spelling in (source, source.resolve()):
                    for suffix in ("", "-wal", "-shm"):
                        protected = Path(str(spelling) + suffix)
                        if (args.json_path.resolve() == protected.resolve()
                                or (args.json_path.exists() and protected.exists()
                                    and args.json_path.samefile(protected))):
                            raise ValueError('--json must not overwrite an input database or its companion files')
        report = compare_databases(args.a,args.b,dates=args.dates.split(',') if args.dates else (),
                                   allow=args.allow.split(',') if args.allow else (),samples=args.samples)
        rendered = json.dumps(report,indent=2,ensure_ascii=False)
        if args.json_path:
            args.json_path.write_text(rendered+'\n')
        print(rendered)
        return 0 if report['identical'] else 1
    except (ValueError,OSError,sqlite3.Error) as exc:
        print(f'comparison error: {exc}',file=__import__('sys').stderr)
        return 2
