"""Bounded raw-response evidence for September15 Munich stops and RE161xx controls."""
from pathlib import Path
from collections import Counter, defaultdict
import csv, hashlib, json, resource, sqlite3, sys, time
import pyarrow.parquet as pq
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from dbrel.ingest.parser import parse_timetable,run_key
from dbrel.ingest.archive import _to_epoch,eva_of
from explore_compact_delay import guard
OUT=ROOT/'data/ml/delay_shift_audit_20261013'
MUNICH=(9041,9056,9052,9046,9057,9044)

def main():
 cg=guard();started=time.monotonic()
 if int((cg/'memory.max').read_text())>2*2**30:raise ValueError('private <=2GiB required')
 if (OUT/'raw-receipt-result.json').exists():raise ValueError('completed audit immutable')
 c=sqlite3.connect(f'file:{ROOT/"data/dbrel.sqlite"}?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA cache_size=-32768')
 query='''SELECT ts.id,t.source_run_key,t.train_id,tr.display_name AS train,ts.stop_sequence,st.eva,st.name AS station,
 ts.supposed_arrival_utc AS scheduled,ts.actual_arrival_utc AS actual,ts.arr_delay_s
 FROM trip t CROSS JOIN trip_stop ts ON ts.trip_id=t.id JOIN train tr ON tr.id=t.train_id JOIN station st ON st.id=ts.station_id
 WHERE t.service_date='2026-09-15' AND t.source='tts' AND ts.quality='RECORDED' AND ts.cancelled=0
 AND ts.arr_cancelled=0 AND ts.arr_delay_s IS NOT NULL AND ABS(ts.arr_delay_s)<=7200 AND ts.is_holiday IS NOT NULL AND ts.supposed_arrival_utc IS NOT NULL
 AND (st.id IN (9041,9056,9052,9046,9057,9044) OR tr.display_name LIKE 'RE 161%')'''
 targets={}
 for row in c.execute(query):
  r=dict(row);key=(r['source_run_key'],r['stop_sequence'],r['eva'])
  if key in targets:raise ValueError('ambiguous source run/station key')
  targets[key]=r
 evas={k[2] for k in targets if k[2] is not None};seen=set();traces=[];message_history=defaultdict(list);records=Counter();files=[]
 for d in (14,15,16):files.extend(sorted((ROOT/f'data/archive/raw_data/year=2026/month=9/day={d}').glob('*.parquet')))
 manifest=[]
 for path in files:
  stat=path.stat();manifest.append({'path':str(path.relative_to(ROOT)),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns})
  handle=pq.ParquetFile(path)
  for batch in handle.iter_batches(batch_size=16,columns=['timestamp','url','api_name','response_data','status_code']):
   for row in batch.to_pylist():
    if str(row['status_code'])!='200' or not str(row['api_name']).endswith('/fchg'):continue
    eva=eva_of(row['url'] or '')
    if eva not in evas:continue
    recorded=_to_epoch(row['timestamp']);body=row['response_data']
    if not body or recorded is None:continue
    records['target_station_responses']+=1
    raw=body.encode() if isinstance(body,str) else body
    parsed=parse_timetable(raw)
    for stop in parsed.stops:
     key=(run_key(stop.stop_id),stop.stop_index,eva)
     if key not in targets:continue
     target=targets[key];seen.add(key);records['matched_stop_reports']+=1
     event=stop.arrival
     if event and event.planned_ts is not None and event.changed_ts is not None:
      delta=(event.changed_ts-event.planned_ts)/60
      records['raw_negative_reports']+=delta < -5
      if delta < -5 and len(traces)<80:
       traces.append({**target,'archive_recorded_at':recorded,'raw_planned_utc':event.planned_ts,'raw_changed_utc':event.changed_ts,'raw_delay_min':delta,'database_delay_min':target['arr_delay_s']/60,'body_sha256':hashlib.sha256(raw).hexdigest(),'partition':str(path.relative_to(ROOT))})
     for msg in stop.messages:
      if msg.msg_id is None:records['message_missing_id']+=1;continue
      message_history[key].append({'recorded':recorded,'msg_id':msg.msg_id,'source_ts':msg.ts,'type':msg.msg_type,'category':msg.category,'deleted':msg.deleted,'from':msg.valid_from,'to':msg.valid_to})
  print('Raw partition',path.name,'target responses',records['target_station_responses'],flush=True)
 results=[]
 for key,target in targets.items():
  cutoff=target['scheduled']-5*3600;latest={};retrospective=False;early_seen=False
  for msg in sorted(message_history[key],key=lambda x:(x['recorded'],x['source_ts'] if x['source_ts'] is not None else -1)):
   cat=msg['category'] or ''
   if msg['type']=='h' and cat.startswith('Bauarbeiten') and not msg['deleted']:retrospective=True
   if msg['recorded']<=cutoff:
    early_seen=True;latest[msg['msg_id']]=msg
  valid=[]
  for msg in latest.values():
   if msg['type']=='h' and (msg['category'] or '').startswith('Bauarbeiten') and not msg['deleted'] and msg['from'] is not None and msg['to'] is not None and msg['from']<=target['scheduled']<=msg['to']:valid.append(msg)
  results.append({**target,'archive_stop_observed':key in seen,'recorded_works_anytime':retrospective,'any_message_observed_before_cutoff':early_seen,'valid_works_observed_before_cutoff':bool(valid),'first_works_receipt':min((m['recorded'] for m in message_history[key] if m['type']=='h' and (m['category'] or '').startswith('Bauarbeiten')),default=None)})
 def write(name,rows):
  if rows:
   with (OUT/name).open('w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 write('raw-early-label-traces.csv',traces);write('raw-receipt-stop-coverage.csv',results)
 result={'target_stops':len(targets),'archive_observed_stops':sum(r['archive_stop_observed'] for r in results),'works_recorded_anytime_stops':sum(r['recorded_works_anytime'] for r in results),'works_verified_before_cutoff_stops':sum(r['valid_works_observed_before_cutoff'] for r in results),'any_message_before_cutoff_stops':sum(r['any_message_observed_before_cutoff'] for r in results),'counts':dict(records),'partitions':manifest,'limitations':['Targeted Munich six hotspot stations plus RE161xx stops on September15, not national feature availability','Archived recording time proves observation at the archive source, not delivery to the running app','Only retained September14–16 partitions scanned; earlier publications or unseen responses may be missing','Latest observed message versions as of cutoff; source versions tied within one recorded instant require further ambiguity audit before predictive use','No recorded works is not proof of no works'],'resources':{'seconds':time.monotonic()-started,'cgroup_peak_bytes':int((cg/'memory.peak').read_text()),'rss_peak_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'swap_max':(cg/'memory.swap.max').read_text().strip()}}
 (OUT/'raw-receipt-result.json').write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='partitions'}),flush=True)
if __name__=='__main__':main()
