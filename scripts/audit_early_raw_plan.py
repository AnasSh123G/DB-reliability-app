"""Join raw plan timestamps to raw changes for the suspicious regional services."""
from pathlib import Path
from collections import defaultdict,Counter
import csv,json,sys,hashlib
import pyarrow.parquet as pq
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from dbrel.ingest.parser import parse_timetable,run_key
from dbrel.ingest.archive import _to_epoch,eva_of
from explore_compact_delay import guard
OUT=ROOT/'data/ml/delay_shift_audit_20261013'
def main():
 cg=guard()
 if int((cg/'memory.max').read_text())>2*2**30:raise ValueError('<=2GiB required')
 if (OUT/'raw-plan-reconciliation.json').exists():raise ValueError('completed immutable')
 rows=list(csv.DictReader((OUT/'raw-receipt-stop-coverage.csv').open()));targets={(r['source_run_key'],int(r['stop_sequence']),int(r['eva'])):r for r in rows if r['train'].startswith('RE 161') and int(r['arr_delay_s']) < -300}
 evas={k[2] for k in targets};plans=defaultdict(set);changes=defaultdict(list);counts=Counter()
 for d in (14,15,16):
  for path in sorted((ROOT/f'data/archive/raw_data/year=2026/month=9/day={d}').glob('*.parquet')):
   for batch in pq.ParquetFile(path).iter_batches(batch_size=16,columns=['timestamp','url','api_name','response_data','status_code']):
    for row in batch.to_pylist():
     if str(row['status_code'])!='200' or eva_of(row['url'] or '') not in evas:continue
     body=row['response_data'];epoch=_to_epoch(row['timestamp'])
     if not body or epoch is None:continue
     raw=body.encode() if isinstance(body,str) else body
     for stop in parse_timetable(raw).stops:
      key=(run_key(stop.stop_id),stop.stop_index,eva_of(row['url'] or ''))
      if key not in targets or not stop.arrival:continue
      e=stop.arrival
      if e.planned_ts is not None:plans[key].add(e.planned_ts)
      if e.changed_ts is not None:changes[key].append((epoch,e.changed_ts,hashlib.sha256(raw).hexdigest(),str(path.relative_to(ROOT))))
 examples=[]
 for key,r in targets.items():
  pp=plans[key];cc=changes[key];expected=int(r['scheduled']);actual=int(r['actual'])
  counts['target_negative_stops']+=1;counts['stops_with_raw_plan']+=bool(pp);counts['stops_with_raw_changed_time']+=bool(cc)
  counts['raw_plan_matches_database_schedule']+=expected in pp
  counts['raw_changed_matches_database_actual']+=any(t==actual for _,t,_,_ in cc)
  counts['source_confirmed_negative_pairs']+=expected in pp and any(t==actual for _,t,_,_ in cc)
  example=next((z for z in cc if z[1]==actual),cc[-1] if cc else None)
  examples.append({**r,'raw_plan_utc_values':';'.join(map(str,sorted(pp))),'raw_changed_matches_database_actual':any(t==actual for _,t,_,_ in cc),'raw_plan_matches_database_schedule':expected in pp,'raw_joined_delay_min':(actual-expected)/60 if expected in pp and any(t==actual for _,t,_,_ in cc) else None,'changed_response_sha256':example[2] if example else None,'changed_partition':example[3] if example else None})
 if examples:
  with (OUT/'raw-early-plan-reconciliation.csv').open('w',newline='') as f:
   w=csv.DictWriter(f,fieldnames=list(examples[0]));w.writeheader();w.writerows(examples)
 result={'counts':dict(counts),'scope':'negative RE161xx September15 stops in targeted receipt cohort','limitations':['Plan and changes are joined by exact source run, stop sequence and station EVA','A source-confirmed negative pair establishes feed arithmetic, not physical realization or perfect route matching','Distinct raw plan values retained; no schedule version overwritten'],'cgroup_peak_bytes':int((cg/'memory.peak').read_text())}
 (OUT/'raw-plan-reconciliation.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
if __name__=='__main__':main()
