"""Update the August-only matched comparison with verified target overlays.

Retains the exact original matched population, missing values and all August
dates. Rows outside the frozen corrected cache remain explicitly unverified;
in particular August 22 has no eligible prepared training rows.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from dbrel.ml.full_year import PreparedSet
from recency_provenance import file_hash
from verify_correction_version import OUT as CORRECTION, PREFIX, verify
from explore_compact_delay import guard

OLD = ROOT/'data/ml/delay_shift_audit_20261013'
OUT = ROOT/'data/ml/recency_training_20261013/corrected_august_comparison'
DATES = [f'2026-08-{d:02d}' for d in range(1,32)] + [f'2026-09-{d:02d}' for d in range(6,20)]


def trip_comparison(values):
    """One vote per August date, fixed common stops and >=80% run coverage."""
    values = np.asarray(values)
    common = np.isfinite(values[:, :31]).any(axis=1)
    if not common.any(): return None
    same_stops = values[common]
    observed = np.isfinite(same_stops)
    accepted = observed.sum(axis=0) >= np.ceil(.8*len(same_stops))
    daily = np.full(values.shape[1], np.nan)
    for j in np.flatnonzero(accepted): daily[j] = same_stops[observed[:,j],j].mean()
    august = daily[:31][np.isfinite(daily[:31])]
    september = daily[DATES.index('2026-09-15')]
    if not len(august) or not np.isfinite(september): return None
    return {'august_dates':len(august), 'supported':len(august)>=10, 'common_stops':int(common.sum()),
            'august_mean_min':float(august.mean()), 'september15_mean_min':float(september),
            'deviation_min':float(september-august.mean()),
            'august_std_min':float(august.std(ddof=1)) if len(august)>1 else None,
            'daily_deviations':{DATES[j]:float(daily[j]-august.mean()) for j in range(31,len(DATES)) if np.isfinite(daily[j])}}


def main():
    cg = guard()
    if int((cg/'memory.max').read_text()) > 2*2**30: raise ValueError('<=2GiB required')
    verify()
    evidence = json.loads((OLD/'publication-verification.json').read_text())['content_identities']
    matrix_name = 'data/ml/delay_shift_audit_20261013/matched-delays.f32'
    if file_hash(ROOT/matrix_name) != evidence[matrix_name]['sha256']:
        raise ValueError('original matched matrix changed')
    source = json.loads((CORRECTION/'source-inputs.json').read_text())
    if source['database']['sha256'] != evidence['data/dbrel.sqlite']['sha256']:
        raise ValueError('original comparison and correction used different source databases')
    database = ROOT/'data/dbrel.sqlite'; wal = Path(str(database)+'-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('source database WAL is nonempty')
    db_stat = database.stat()
    if file_hash(database) != source['database']['sha256']:
        raise ValueError('source database changed')
    conn = sqlite3.connect(f'file:{ROOT / "data/dbrel.sqlite"}?mode=ro',uri=True)
    conn.execute('PRAGMA cache_size=-32768'); conn.execute('BEGIN')
    version = conn.execute('PRAGMA data_version').fetchone()[0]
    if database.stat().st_mtime_ns != db_stat.st_mtime_ns or (wal.exists() and wal.stat().st_size):
        raise ValueError('source database changed before snapshot')
    prepared = {role:PreparedSet(CORRECTION/'prepared'/f'{PREFIX}{role}') for role in ('fit','eval')}
    data = prepared['eval']; day = data.column('day')
    epoch = int(np.datetime64('2026-09-15','D').astype(int))
    # The scalar day column is small; slice copies stay bounded to one date.
    days = np.memmap(data.dir/'day.bin',mode='r',dtype='<i4',shape=(len(data),))
    a,b = int(np.searchsorted(days,epoch)),int(np.searchsorted(days,epoch,side='right'))
    wanted = set(map(int,data.column('stop_id')[a:b]))
    rows = [row for row in conn.execute('''SELECT ts.id,t.train_id,ts.stop_sequence,ts.station_id,tr.display_name,
        tr.segment,s.state FROM trip t CROSS JOIN trip_stop ts ON ts.trip_id=t.id
        JOIN train tr ON tr.id=t.train_id JOIN station s ON s.id=ts.station_id WHERE t.service_date=?''',('2026-09-15',)) if row[0] in wanted]
    rows.sort(key=lambda r:(r[1],r[2],r[3]))
    if len(rows) != len(wanted): raise ValueError('September ordered population differs')
    index = {(r[1],r[2],r[3]):i for i,r in enumerate(rows)}
    if len(index)!=len(rows): raise ValueError('ambiguous matched key')
    shape = (len(rows),len(DATES))
    original = np.memmap(ROOT/matrix_name,mode='r',dtype='<f4',shape=shape)
    old_labels = np.fromfile(ROOT/'data/ml/full_year/prepared'/f'{PREFIX}eval'/'y.bin',
                             dtype='<f4',count=b-a,offset=a*4)
    old_map = dict(zip(map(int,data.column('stop_id')[a:b]),map(float,old_labels)))
    if any(abs(float(original[i,DATES.index('2026-09-15')])-old_map[row[0]])>1e-4
           or not np.isfinite(original[i,DATES.index('2026-09-15')]) for i,row in enumerate(rows)):
        raise ValueError('original matrix row order differs from the September prepared targets')
    del old_map,old_labels,wanted
    OUT.mkdir(parents=True,exist_ok=True)
    matrix = np.memmap(OUT/'matched-delays.f32',mode='w+',dtype='<f4',shape=shape)
    verified = np.memmap(OUT/'matched-verified.u8',mode='w+',dtype='u1',shape=shape)
    for begin in range(0,len(rows),8192): matrix[begin:begin+8192] = original[begin:begin+8192]
    verified[:] = 0
    coverage = []
    for j,date in enumerate(DATES):
        epoch = int(np.datetime64(date,'D').astype(int)); role = 'fit' if date <= '2026-08-21' else 'eval'
        data = prepared[role]; days = np.memmap(data.dir/'day.bin',mode='r',dtype='<i4',shape=(len(data),))
        a,b = int(np.searchsorted(days,epoch)),int(np.searchsorted(days,epoch,side='right'))
        ids = data.column('stop_id')[a:b]; labels = data.y[a:b]
        statuses = np.memmap(data.dir/'label_status.u8',mode='r',dtype='u1',shape=(len(data),))[a:b]
        lookup = {int(sid):(float(labels[k]),int(statuses[k])) for k,sid in enumerate(ids)}
        matched = changed = certified = 0
        for sid,train,seq,station,old_delay in conn.execute('''SELECT ts.id,t.train_id,ts.stop_sequence,ts.station_id,ts.arr_delay_s
            FROM trip t CROSS JOIN trip_stop ts ON ts.trip_id=t.id WHERE t.service_date=?''',(date,)):
            i = index.get((train,seq,station))
            if i is None or sid not in lookup or not np.isfinite(original[i,j]): continue
            if old_delay is None or abs(original[i,j]-old_delay/60)>1e-4:
                raise ValueError('original matched value differs from source identity')
            matched += 1; label,status = lookup[sid]
            if status in (1,2):
                matrix[i,j] = label; verified[i,j] = 1; certified += 1
                changed += status == 2
        n = int(np.isfinite(original[:,j]).sum())
        coverage.append({'date':date,'original_observed_matched_stops':n,'in_corrected_cache':matched,
                         'source_verified_stops':certified,'corrected_stops':changed,
                         'unverified_retained_original_stops':n-certified})
        print(date,json.dumps(coverage[-1]),flush=True)
    matrix.flush(); verified.flush()
    groups = defaultdict(list)
    for i,row in enumerate(rows): groups[row[1]].append(i)
    records = []
    with (OUT/'stop-comparison.csv').open('w',newline='') as stops:
        writer = csv.DictWriter(stops,fieldnames=['train_id','stop_sequence','station_id','september_stop_id',
            'train','segment','state','august_dates','august_mean_min','september15_min','deviation_min',
            'august_source_verified_dates','september_source_verified'])
        writer.writeheader()
        for train,indices in groups.items():
            scored = trip_comparison(matrix[indices,:])
            if scored is None: continue
            first = rows[indices[0]]
            record = {'train_id':train,'train':first[4],'segment':first[5],'first_stop_state':first[6],
                      **{k:v for k,v in scored.items() if k!='daily_deviations'}}
            records.append(record)
            for i in indices:
                august = matrix[i,:31]; observed=np.isfinite(august)
                if not observed.any(): continue
                row = rows[i]; september=float(matrix[i,DATES.index('2026-09-15')]);mean=float(august[observed].mean())
                writer.writerow({'train_id':train,'stop_sequence':row[2],'station_id':row[3],'september_stop_id':row[0],
                    'train':row[4],'segment':row[5],'state':row[6],'august_dates':int(observed.sum()),
                    'august_mean_min':mean,'september15_min':september,'deviation_min':september-mean,
                    'august_source_verified_dates':int(verified[i,:31][observed].sum()),
                    'september_source_verified':int(verified[i,DATES.index('2026-09-15')])})
    with (OUT/'trip-comparison.csv').open('w',newline='') as output:
        writer=csv.DictWriter(output,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)
    supported = [r for r in records if r['supported']]
    deviations = np.array([r['deviation_min'] for r in supported])
    conn.rollback()
    if conn.execute('PRAGMA data_version').fetchone()[0] != version: raise ValueError('database changed during comparison')
    if database.stat().st_mtime_ns != db_stat.st_mtime_ns or (wal.exists() and wal.stat().st_size):
        raise ValueError('source database changed during comparison')
    conn.close()
    config = OUT/'matplotlib'; config.mkdir(exist_ok=True)
    os.environ['MPLCONFIGDIR'] = str(config)
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    fig, ax = plt.subplots(figsize=(9,4))
    ax.hist(deviations,bins=np.linspace(-30,60,91),color='#267b8e')
    ax.axvline(0,color='#444444',linewidth=1)
    ax.set(xlabel='September 15 trip mean − August 1–31 trip mean (minutes)',
           ylabel='Supported recurring trips',title='Verified corrections overlaid; uncovered targets retained')
    fig.tight_layout()
    fig.savefig(OUT/'deviation.png',dpi=160); fig.savefig(OUT/'deviation.svg'); plt.close(fig)
    result = {'reference':'August1–31 only, one vote per recurring trip/date with fixed common stops',
              'script_sha':file_hash(Path(__file__)),
              'label_policy':'verified corrected targets overlaid; uncovered rows retain explicitly unverified originals',
              'correction_result_sha':file_hash(CORRECTION/'result.json'),'original_matrix_sha':file_hash(ROOT/matrix_name),
              'supported_trips':len(supported),'mean_deviation_min':float(deviations.mean()),
              'median_deviation_min':float(np.median(deviations)),
              'shares':{f'over_{t}_min':float((deviations>t).mean()) for t in (5,15,30)},
              'coverage':coverage,'files':{p.name:file_hash(p) for p in OUT.glob('*') if p.is_file() and p.name!='result.json'},
              'limitations':['August22 lacks prepared correction rows; coverage is partial, not a fully corrected month',
                             f'Histogram shows −30..60 minutes; {int(((deviations < -30)|(deviations > 60)).sum())} supported trips outside that range remain in all aggregate metrics',
                             'no new forecast-time schedule/cause features; descriptive reused cohort']}
    (OUT/'result.json').write_text(json.dumps(result,indent=2))
    report = ROOT/'docs/corrected-august-comparison.md'
    report.write_text('\n'.join([
        '# September 15 versus August after verified target corrections','',
        'The sole reference is August 1–31. Each accepted August run gets one vote over fixed common stops, with at least 80% observed. Services need at least ten August dates for the supported summary.','',
        f"Supported recurring trips: **{len(supported):,}**. Mean signed deviation: **{deviations.mean():+.2f} minutes**; median: **{np.median(deviations):+.2f} minutes**.",'',
        '| Above August mean by | Supported trips |','| --- | --- |',
        *[f'| {threshold} minutes | {100*(deviations>threshold).mean():.2f}% |' for threshold in (5,15,30)],'',
        'This is a partially verified overlay, not a wholly corrected month. August 22 has no eligible prepared correction rows; uncovered observations retain their original targets and are counted explicitly. Neither missing nor cancelled arrivals become zero.','',
        '![Signed trip deviations](../data/ml/recency_training_20261013/corrected_august_comparison/deviation.png)','',
        result['limitations'][1], '',
        '[Per-trip table](../data/ml/recency_training_20261013/corrected_august_comparison/trip-comparison.csv) · [Per-stop table](../data/ml/recency_training_20261013/corrected_august_comparison/stop-comparison.csv) · [Coverage and identities](../data/ml/recency_training_20261013/corrected_august_comparison/result.json)','']))
    print('CORRECTED_AUGUST_COMPARISON',json.dumps({k:result[k] for k in ('supported_trips','mean_deviation_min','median_deviation_min','shares')}),flush=True)


if __name__=='__main__': main()
