"""Recover completed comparison artifacts after a final CSV-schema failure."""
from pathlib import Path
from collections import Counter,defaultdict
import ast,csv,json,re,sqlite3,sys,time,resource
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from explore_compact_delay import guard
OUT=ROOT/'data/ml/delay_shift_audit_20261013'

def main():
 cg=guard();started=time.monotonic()
 if int((cg/'memory.max').read_text())>2*2**30:raise ValueError('<=2GiB required')
 if (OUT/'result.json').exists():raise ValueError('completed result immutable')
 text=(OUT/'job-v2.log').read_text();audit=ast.literal_eval(next(s.split('Early-label audit ',1)[1] for s in text.splitlines() if s.startswith('Early-label audit ')))
 coverage={r['date']:r for r in csv.DictReader((OUT/'date-coverage.csv').open())}
 dates=[f'2026-08-{d:02d}' for d in range(1,32)]+[f'2026-09-{d:02d}' for d in range(6,20)]
 for line in text.splitlines():
  m=re.fullmatch(r'(\d{4}-\d{2}-\d{2}) eligible (\d+) exact matches (\d+) messages (\d+)',line)
  if m:
   d,n,matched,msgs=m.groups();coverage.setdefault(d,{'date':d,'eligible_arrivals':int(n),'exact_stop_matched':int(matched),'raw_message_versions':int(msgs),'matched_trips_with_valid_works':None})
 if set(coverage)!=set(dates):raise ValueError('incomplete matrix pass; cannot recover')
 fields=list(dict.fromkeys(k for r in coverage.values() for k in r))
 with (OUT/'date-coverage.csv').open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(coverage[d] for d in dates)
 trips=list(csv.DictReader((OUT/'trip-comparison.csv').open()));supported=[r for r in trips if r['supported']=='True']
 c=sqlite3.connect(f'file:{ROOT/"data/dbrel.sqlite"}?mode=ro',uri=True);c.execute('PRAGMA cache_size=-32768');c.execute('BEGIN')
 pre=ROOT/'data/ml/full_year/prepared/search-2026-09-26-robust-no-doy-mc20-rd56-eval';n=json.loads((pre/'meta.json').read_text())['n'];day=np.memmap(pre/'day.bin',dtype='<i4',mode='r',shape=(n,));ids=np.memmap(pre/'stop_id.bin',dtype='<i8',mode='r',shape=(n,))
 d=int(np.datetime64('2026-09-15','D').astype(int));a=np.searchsorted(day,d);b=np.searchsorted(day,d,side='right');wanted=set(np.asarray(ids[a:b]).tolist())
 keys=[r for r in c.execute("SELECT t.train_id,ts.stop_sequence,ts.station_id,ts.id FROM trip t CROSS JOIN trip_stop ts ON ts.trip_id=t.id WHERE t.service_date='2026-09-15'") if r[3] in wanted];keys.sort()
 if len(keys)!=len(wanted):raise ValueError('matrix row reconstruction failed')
 index={(train,seq,station):i for i,(train,seq,station,sid) in enumerate(keys)}
 vals=np.memmap(OUT/'matched-delays.f32',dtype='<f4',mode='r',shape=(len(keys),len(dates)))
 groups=defaultdict(list)
 for s in csv.DictReader((OUT/'stop-comparison.csv').open()):
  key=(int(s['train_id']),int(s['stop_sequence']),int(s['station_id']));i=index[key]
  if int(s['september_stop_id'])!=keys[i][3]:raise ValueError('saved CSV ordered identity differs')
  ar=np.asarray(vals[i,:31]);ar=ar[np.isfinite(ar)]
  if len(ar)!=int(s['august_dates']) or abs(float(ar.mean())-float(s['august_mean_min']))>1e-5:raise ValueError('saved August mean differs from completed matrix')
  groups[key[0]].append(i)
 trend=defaultdict(list)
 for r in supported:
  v=np.asarray(vals[groups[int(r['train_id'])],:])
  for j,d in enumerate(dates[31:],31):
   kept=np.isfinite(v[:,j])
   if kept.sum()>=np.ceil(.8*len(v)):trend[d].append(float(v[kept,j].mean())-float(r['august_mean_min']))
 def summary(rs):
  ds=np.array([float(r['deviation_min']) for r in rs]);return {'trips':len(rs),'mean_deviation_min':float(ds.mean()) if len(ds) else None,'median_deviation_min':float(np.median(ds)) if len(ds) else None,**{f'share_over_{t}_min':float((ds>t).mean()) if len(ds) else None for t in (5,15,30)}}
 inventory=Counter();cause_audit=Counter()
 for d in dates:
  for typ,code,cat,ts,vfrom,vto,deleted in c.execute('SELECT m.msg_type,m.code,m.category,m.ts,m.valid_from,m.valid_to,m.deleted FROM trip t CROSS JOIN tt_message m ON m.trip_id=t.id WHERE t.service_date=?',(d,)):
   inventory[(d[:7],str(typ),str(code),str(cat))]+=1
   cause_audit['raw_versions']+=1;cause_audit['missing_source_ts']+=ts is None;cause_audit['missing_validity_bound']+=vfrom is None or vto is None;cause_audit['deleted_versions']+=bool(deleted)
 with (OUT/'cause-inventory.csv').open('w',newline='') as f:
  w=csv.writer(f);w.writerow(['month','type','code','category','raw_versions']);w.writerows([*k,v] for k,v in inventory.items())
 earlygroups=defaultdict(list)
 for r in csv.DictReader((OUT/'early-label-groups.csv').open()):earlygroups[r['group']].append({'key':r['key'],'threshold':int(r['threshold_minutes']),'rows':int(r['arrivals'])})
 regions=sorted({r['region_of_first_matched_stop'] for r in supported});segments=sorted({r['segment'] for r in supported})
 result={'early_audit':audit,'early_groups':dict(earlygroups),'september15_prepared_arrivals':len(wanted),'september15_recurring_trips':len({r[0] for r in keys}),'comparison':summary(supported),'all_matched':summary(trips),'by_segment':{k:summary([r for r in supported if r['segment']==k]) for k in segments},'by_first_stop_region':{k:summary([r for r in supported if r['region_of_first_matched_stop']==k]) for k in regions},'daily_matched_deviation':{d:{'trips':len(v),'mean_min':float(np.mean(v)),'median_min':float(np.median(v))} for d,v in trend.items()},'cause_audit':dict(cause_audit),'recovery':{'completed_45_day_matrix':True,'trip_and_stop_csvs_reused':True,'all_saved_stop_august_means_reconciled':True,'missing_daily_valid_works_counts':'Unavailable after CSV failure for later dates; represented as missing, never zero'},'resources':{'seconds':time.monotonic()-started,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'cgroup_peak_bytes':max(1574842368,int((cg/'memory.peak').read_text())),'swap_max':(cg/'memory.swap.max').read_text().strip()}}
 (OUT/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result['comparison']),flush=True)
if __name__=='__main__':main()
