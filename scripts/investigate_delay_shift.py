"""Read-only early-label audit and September 15 versus August-only comparison."""
from __future__ import annotations
import csv, hashlib, json, os, resource, sqlite3, sys, time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from dbrel.ml.dataset import _where
from explore_compact_delay import guard
OUT=ROOT/'data/ml/delay_shift_audit_20261013'
PRE=ROOT/'data/ml/full_year/prepared/search-2026-09-26-robust-no-doy-mc20-rd56-eval'
REF=ROOT/'data/ml/full_year/runs/compact-2026-09-26/trials/f1ac12b1742e3a0b/validation.npz'

def identity(p):
 s=p.stat();return {'path':str(p.resolve()),'bytes':s.st_size,'mtime_ns':s.st_mtime_ns}
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def csv_write(name,rows):
 if not rows:return
 with (OUT/name).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(dict.fromkeys(k for row in rows for k in row)));w.writeheader();w.writerows(rows)
def connect():
 c=sqlite3.connect(f'file:{ROOT/"data/dbrel.sqlite"}?mode=ro',uri=True)
 c.execute('PRAGMA cache_size=-65536');c.execute('PRAGMA temp_store=FILE');return c

def eligible(r):
 return (r['quality']=='RECORDED' and not r['cancelled'] and not r['arr_cancelled'] and
         r['holiday'] is not None and r['scheduled'] is not None and r['delay_s'] is not None and abs(r['delay_s'])<=7200)
QUERY='''SELECT ts.id AS stop_id,ts.trip_id,t.train_id,t.source,t.service_date,t.path_sha1 AS path,
 t.feed_version_id,ts.stop_sequence AS seq,ts.station_id,st.name AS station,st.state,tr.display_name AS train,
 tr.segment,tr.line_id,t.source_run_key,ts.supposed_arrival_utc AS scheduled,ts.actual_arrival_utc AS actual,
 ts.arr_delay_s AS delay_s,ts.quality,ts.cancelled,ts.arr_cancelled,ts.is_holiday AS holiday,
 ts.last_observed_at,t.finalized_at,ts.station_stop_id,ss.planned_arrival_sec,
 ss.path_sha1 AS schedule_path,ss.feed_version_id AS schedule_feed
 FROM trip t CROSS JOIN trip_stop ts ON ts.trip_id=t.id
 JOIN train tr ON tr.id=t.train_id JOIN station st ON st.id=ts.station_id
 LEFT JOIN station_stop ss ON ss.id=ts.station_stop_id'''

def main():
 started=time.monotonic();cg=guard()
 if int((cg/'memory.max').read_text())>2*2**30:raise ValueError('private <=2GiB cap required')
 OUT.mkdir(parents=True,exist_ok=True)
 if (OUT/'result.json').exists():raise ValueError('immutable completed audit; choose new version')
 c=connect();c.row_factory=sqlite3.Row
 db_before=identity(ROOT/'data/dbrel.sqlite')
 manifest={'version':1,'spec_sha':digest(ROOT/'docs/delay-recency-and-cause-spec.md'),
 'script_sha':digest(Path(__file__)),'source_database':db_before,'prepared_meta_sha':digest(PRE/'meta.json'),
 'reference':identity(REF),'prepared_files':{x:identity(PRE/f'{x}.bin') for x in ('day','y','stop_id')},
 'comparison':'September 15 minus August 1–31 only; exact train/source/path and station/sequence matches',
 'training_performed':False,'label_changes':False}
 (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2))
 n=json.loads((PRE/'meta.json').read_text())['n'];days=np.memmap(PRE/'day.bin',dtype='<i4',mode='r',shape=(n,))
 y=np.memmap(PRE/'y.bin',dtype='<f4',mode='r',shape=(n,));ids=np.memmap(PRE/'stop_id.bin',dtype='<i8',mode='r',shape=(n,))
 lo=int(np.searchsorted(days,int(np.datetime64('2026-09-06').astype('datetime64[D]').astype(int))))
 early_idx=lo+np.flatnonzero(y[lo:] < -5);early_ids=np.asarray(ids[early_idx]);early_y=np.asarray(y[early_idx]);
 with np.load(REF) as z:
  ref_ids=z['stop_id'];pred=z['point']
 for a in range(0,n,131072):
  if not np.array_equal(ref_ids[a:a+131072],ids[a:a+131072]):raise ValueError('reference identity mismatch')
 early_pred=pred[early_idx];del pred,ref_ids
 early_map={int(k):(float(v),float(p)) for k,v,p in zip(early_ids,early_y,early_pred)}
 audit=Counter();groups=defaultdict(lambda:Counter());examples=[];extreme_trains=Counter();found=set()
 for a in range(0,len(early_ids),400):
  batch=early_ids[a:a+400].tolist()
  q=QUERY.replace('FROM trip t CROSS JOIN trip_stop ts ON ts.trip_id=t.id', 'FROM trip_stop ts JOIN trip t ON t.id=ts.trip_id')+f' WHERE ts.id IN ({",".join("?" for _ in batch)})'
  for row in c.execute(q,batch):
   r=dict(row);found.add(r['stop_id']);target,point=early_map[r['stop_id']]
   shown=0 if point<1 else np.floor(point+.5);derived=None if r['actual'] is None or r['scheduled'] is None else (r['actual']-r['scheduled'])/60
   label_consistent=r['delay_s'] is not None and abs(target-r['delay_s']/60)<1e-4
   timestamp_consistent=derived is not None and abs(derived-r['delay_s']/60)<1e-4
   audit['rows']+=1;audit['prepared_vs_database_mismatch']+=not label_consistent
   audit['actual_timestamp_missing']+=derived is None;audit['timestamp_vs_delay_mismatch']+=derived is not None and not timestamp_consistent
   extreme_trains[r['train_id']]+=1
   for threshold in (5,15,30,60):
    if target < -threshold:
     audit[f'below_minus_{threshold}']+=1
     for level,key in [('date',r['service_date']),('source',r['source']),('state',r['state']),('train',r['train']),('station',r['station']),('sequence',str(r['seq'])),('feed',str(r['feed_version_id'])),('path',str(r['path'])),('quality',r['quality'])]:
      groups[level][(key,threshold)]+=1
   if len(examples)<80 or (r['train'] or '').startswith('RE 161') and len(examples)<220:
    examples.append({**r,'prepared_target_min':target,'prediction_raw_min':point,'prediction_display_min':float(shown),'reconstructed_delay_min':derived,'prepared_consistent':label_consistent,'timestamp_consistent':timestamp_consistent,'classification':'source-consistent but genuine-versus-matching-error unresolved' if timestamp_consistent and label_consistent else 'requires source reconciliation'})
 if found!=set(early_map):raise ValueError('early stop IDs missing in database')
 csv_write('early-label-examples.csv',examples)
 csv_write('early-label-groups.csv',[{'group':g,'key':key,'threshold_minutes':-threshold,'arrivals':count} for g,counts in groups.items() for (key,threshold),count in counts.items()])
 print('Early-label audit',dict(audit),flush=True)
 day15=int(np.datetime64('2026-09-15').astype('datetime64[D]').astype(int));a=int(np.searchsorted(days,day15));b=int(np.searchsorted(days,day15,side='right'))
 sep_target={int(k):float(v) for k,v in zip(ids[a:b],y[a:b])}
 rows=[]
 for row in c.execute(QUERY+' WHERE t.service_date=?',('2026-09-15',)):
  if row['stop_id'] in sep_target:
   r=dict(row)
   if not eligible(r) or abs(r['delay_s']/60-sep_target[r['stop_id']])>1e-4:raise ValueError('Sept15 prepared/database mismatch')
   rows.append(r)
 if len(rows)!=len(sep_target):raise ValueError('Sept15 row coverage mismatch')
 rows.sort(key=lambda r:(r['train_id'],r['seq'],r['station_id']))
 trains=defaultdict(list);maps={}
 for i,r in enumerate(rows):trains[r['train_id']].append(i)
 for train,ix in trains.items():
  maps[train]={(rows[i]['seq'],rows[i]['station_id']):i for i in ix}
  if len(maps[train])!=len(ix):raise ValueError('ambiguous stop mapping')
 dates=[f'2026-08-{d:02d}' for d in range(1,32)]+[f'2026-09-{d:02d}' for d in range(6,20)]
 values=np.memmap(OUT/'matched-delays.f32',mode='w+',dtype='<f4',shape=(len(rows),len(dates)));values[:]=np.nan
 statuses=np.memmap(OUT/'matched-status.u8',mode='w+',dtype='u1',shape=values.shape);statuses[:]=0
 matching=Counter();daystats=[];cause_trip={};cause_audit=Counter();cause_categories=Counter();cause_examples=[]
 for j,day in enumerate(dates):
  trip_meta={};stats=Counter()
  for raw in c.execute(QUERY+' WHERE t.service_date=?',(day,)):
   r=dict(raw);stats['all_stop_rows']+=1
   if eligible(r):stats['eligible_arrivals']+=1
   if r['train_id'] not in maps:continue
   train=r['train_id'];sr=rows[trains[train][0]]
   trip_meta[r['trip_id']]=(train,r['scheduled'])
   if r['source']!=sr['source'] or not r['path'] or not sr['path'] or r['path']!=sr['path']:
    stats['path_or_source_unmatched']+=1;continue
   i=maps[train].get((r['seq'],r['station_id']))
   if i is None:stats['stop_unmatched']+=1;continue
   stats['exact_stop_matched']+=1
   statuses[i,j]=2 if r['arr_cancelled'] or r['cancelled'] else 1
   if eligible(r):values[i,j]=r['delay_s']/60
   if r['feed_version_id']!=rows[i]['feed_version_id']:stats['feed_version_changed_matched']+=1
   if r['scheduled'] is not None and rows[i]['scheduled'] is not None:
    # Both months are within UTC+2: compare local seconds-of-day without weekday correction.
    diff=(r['scheduled']-rows[i]['scheduled']+43200)%86400-43200
    if abs(diff)>60:stats['scheduled_time_changed_over_1_min']+=1
  latest={};raw_message_count=0
  query='''SELECT m.*,t.train_id FROM trip t CROSS JOIN tt_message m ON m.trip_id=t.id WHERE t.service_date=?'''
  for msg in c.execute(query,(day,)):
   raw_message_count+=1;cause_categories[(day[:7],str(msg['msg_type']),str(msg['code']),str(msg['category']))]+=1
   if msg['trip_id'] not in trip_meta:continue
   key=(msg['trip_id'],msg['stop_sequence'],msg['msg_id'] if msg['msg_id'] is not None else f"missing-id-{msg['id']}")
   order=(msg['ts'] if msg['ts'] is not None else -1,msg['id'])
   if key not in latest or order>latest[key][0]:latest[key]=(order,dict(msg))
  flags=defaultdict(lambda:{'works_trip':False,'disruption_trip':False,'works_stop':False,'disruption_stop':False,'any_recorded':False})
  for _,msg in latest.values():
   train=msg['train_id'];flag=flags[train];flag['any_recorded']=True
   cause_audit['latest_messages']+=1;cause_audit['deleted_latest']+=bool(msg['deleted']);cause_audit['missing_source_ts']+=msg['ts'] is None
   cause_audit['missing_validity_bound']+=msg['valid_from'] is None or msg['valid_to'] is None
   cat=msg['category'] or '';kind='works' if msg['msg_type']=='h' and cat.startswith('Bauarbeiten') else 'disruption' if msg['msg_type']=='h' and cat.startswith(('Störung','Großstörung')) else None
   if not kind or msg['deleted']:continue
   # Require explicit valid bounds. Receipt/as-of state remains unproven here.
   if msg['valid_from'] is None or msg['valid_to'] is None:continue
   ix=trains[train];scheduled=[rows[i]['scheduled']+(int(np.datetime64(day).astype('datetime64[D]').astype(int))-day15)*86400 for i in ix]
   # Approximate day-adjusted Sept timetable; actual August schedule may differ.
   # Stop scope below uses recorded timestamps gathered independently in second lookup.
   stop_windows=c.execute('SELECT supposed_arrival_utc,actual_arrival_utc,stop_sequence FROM trip_stop WHERE trip_id=?',(msg['trip_id'],)).fetchall()
   known=[x[0] for x in stop_windows if x[0] is not None]
   if known and msg['valid_from']<=max(known) and msg['valid_to']>=min(known):flag[kind+'_trip']=True
   for planned,actual,seq in stop_windows:
    if seq==msg['stop_sequence'] and planned is not None:
     start=min(planned,actual) if actual is not None else planned;end=max(planned,actual) if actual is not None else planned
     if msg['valid_from']<=end and msg['valid_to']>=start:flag[kind+'_stop']=True
   if len(cause_examples)<30:cause_examples.append({**msg,'service_date':day,'validity_trip_overlap':flag[kind+'_trip'],'availability':'retrospective only; received-at not stored'})
  daystats.append({'date':day,**dict(stats),'raw_message_versions':raw_message_count,'matched_trips_with_any_message':sum(f['any_recorded'] for f in flags.values()),'matched_trips_with_valid_works':sum(f['works_trip'] for f in flags.values()),'matched_trips_with_valid_stop_works':sum(f['works_stop'] for f in flags.values())})
  for train,flag in flags.items():cause_trip[(train,day)]=flag
  print(day,'eligible',stats['eligible_arrivals'],'exact matches',stats['exact_stop_matched'],'messages',raw_message_count,flush=True)
 values.flush();statuses.flush()
 stop_records=[];trip_records=[];trend=defaultdict(list)
 for train,ix in trains.items():
  aug=np.asarray(values[ix,:31]);common=np.isfinite(aug).any(axis=1);ci=np.asarray(ix)[common];
  if not len(ci):
   matching['trips_without_august_stops']+=1;continue
  v=np.asarray(values[ci,:]);daily=[]
  for j,day in enumerate(dates):
   obs=v[:,j];keep=np.isfinite(obs)
   mean=float(obs[keep].mean()) if keep.sum()>=np.ceil(.8*len(ci)) else None
   daily.append(mean)
  av=np.array([x for x in daily[:31] if x is not None]);sample=len(av)
  if not sample:matching['trips_without_80pct_august_run']+=1;continue
  target=daily[dates.index('2026-09-15')]
  if target is None:matching['sept15_below_common_stop_coverage']+=1;continue
  am=float(av.mean());delta=target-am;sr=rows[ix[0]];flag=cause_trip.get((train,'2026-09-15'),{})
  for j,day in enumerate(dates[31:],31):
   if sample>=10 and daily[j] is not None:trend[day].append(daily[j]-am)
  last_i=max(ci,key=lambda i:rows[i]['seq']);last_aug=np.asarray(values[last_i,:31]);last_aug=last_aug[np.isfinite(last_aug)]
  record={'train_id':train,'train':sr['train'],'source':sr['source'],'segment':sr['segment'],'region_of_first_matched_stop':sr['state'],'path':sr['path'],'september_trip_id':sr['trip_id'],'august_dates':sample,'supported':sample>=10,'common_stops':len(ci),'september15_mean_min':target,'august_mean_min':am,'deviation_min':delta,'august_std_min':float(av.std(ddof=1)) if sample>1 else None,'august_median_min':float(np.median(av)),'august_p90_min':float(np.quantile(av,.9)),'deviation_in_august_sd':delta/float(av.std(ddof=1)) if sample>1 and av.std(ddof=1)>0 else None,'final_common_stop_deviation_min':float(values[last_i,dates.index('2026-09-15')]-last_aug.mean()) if len(last_aug) and np.isfinite(values[last_i,dates.index('2026-09-15')]) else None,'recorded_message':flag.get('any_recorded',False),'valid_works_trip':flag.get('works_trip',False),'valid_works_stop':flag.get('works_stop',False),'valid_disruption_trip':flag.get('disruption_trip',False)}
  trip_records.append(record)
  for i in ci:
   ar=np.asarray(values[i,:31]);ar=ar[np.isfinite(ar)];s=rows[i]
   stop_records.append({'train_id':train,'train':sr['train'],'stop_sequence':s['seq'],'station_id':s['station_id'],'station':s['station'],'state':s['state'],'september_stop_id':s['stop_id'],'august_dates':len(ar),'august_mean_min':float(ar.mean()),'august_std_min':float(ar.std(ddof=1)) if len(ar)>1 else None,'august_median_min':float(np.median(ar)),'august_p90_min':float(np.quantile(ar,.9)),'september15_delay_min':s['delay_s']/60,'deviation_min':s['delay_s']/60-float(ar.mean()),'august_cancelled_matches':int((statuses[i,:31]==2).sum()),'august_absent_matches':int((statuses[i,:31]==0).sum()),'august_present_unobserved':int(((statuses[i,:31]==1)&~np.isfinite(values[i,:31])).sum())})
 csv_write('trip-comparison.csv',trip_records);csv_write('stop-comparison.csv',stop_records);csv_write('date-coverage.csv',daystats);csv_write('cause-message-examples.csv',cause_examples)
 csv_write('cause-inventory.csv',[{'month':m,'type':t,'code':code,'category':cat,'raw_versions':count} for (m,t,code,cat),count in cause_categories.items()])
 supported=[r for r in trip_records if r['supported']]
 def summary(rs):
  ds=np.array([r['deviation_min'] for r in rs]);return {'trips':len(rs),'mean_deviation_min':float(ds.mean()) if len(ds) else None,'median_deviation_min':float(np.median(ds)) if len(ds) else None,**{f'share_over_{t}_min':float((ds>t).mean()) if len(ds) else None for t in (5,15,30)}}
 result={'early_audit':dict(audit),'early_groups':{g:[{'key':key,'threshold':-t,'rows':v} for (key,t),v in cnt.most_common(30)] for g,cnt in groups.items()},'september15_prepared_arrivals':len(rows),'september15_recurring_trips':len(trains),'comparison':summary(supported),'all_matched':summary(trip_records),'unmatched_counts':dict(matching),'by_segment':{k:summary([r for r in supported if r['segment']==k]) for k in sorted({r['segment'] or '' for r in supported})},'by_first_stop_region':{k:summary([r for r in supported if r['region_of_first_matched_stop']==k]) for k in sorted({r['region_of_first_matched_stop'] or '' for r in supported})},'cause_comparison':{k:summary([r for r in supported if r['valid_works_trip']==v]) for k,v in [('valid_works',True),('no_verified_valid_works',False)]},'daily_matched_deviation':{d:{'trips':len(v),'mean_min':float(np.mean(v)),'median_min':float(np.median(v))} for d,v in trend.items()},'cause_audit':dict(cause_audit),'limitations':['August 1–31 only; one vote per August run date','Exact source/train/path/station-sequence matching; unmatched paths/identities not merged','Run means use >=80% observed common stops; observed subsets can differ','Region summaries use first matched stop, not entire geographic route','Retrospective latest-source-timestamp message state; historical receipt still requires raw payload audit','Not independent unseen test; no label corrections or training performed'],'resources':{'seconds':time.monotonic()-started,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'cgroup_peak_bytes':int((cg/'memory.peak').read_text()),'swap_max':(cg/'memory.swap.max').read_text().strip()}}
 if identity(ROOT/'data/dbrel.sqlite')!=db_before:raise ValueError('source database changed during audit')
 (OUT/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result['comparison']),flush=True)
if __name__=='__main__':main()
