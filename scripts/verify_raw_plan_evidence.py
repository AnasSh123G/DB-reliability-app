"""Persist exact raw-plan provenance and substantiate the proposed label policy."""
from pathlib import Path
from collections import defaultdict
import csv,hashlib,json,sys,resource
import numpy as np
import pyarrow.parquet as pq
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from dbrel.ingest.parser import parse_timetable,run_key
from dbrel.ingest.archive import _to_epoch,eva_of
from explore_compact_delay import guard
OUT=ROOT/'data/ml/delay_shift_audit_20261013'
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(2**20),b''):h.update(b)
 return h.hexdigest()
def main():
 cg=guard()
 if int((cg/'memory.max').read_text())>2*2**30:raise ValueError('<=2GiB required')
 if (OUT/'raw-plan-validation.json').exists():raise ValueError('completed immutable')
 rs=list(csv.DictReader((OUT/'raw-early-plan-reconciliation.csv').open()));targets={(r['source_run_key'],int(r['stop_sequence']),int(r['eva'])):r for r in rs}
 evas={k[2] for k in targets};plans=defaultdict(set);evidence=[];identities=[]
 for d in (14,15,16):
  for p in sorted((ROOT/f'data/archive/raw_data/year=2026/month=9/day={d}').glob('*.parquet')):
   st=p.stat();identities.append({'path':str(p.relative_to(ROOT)),'bytes':st.st_size,'mtime_ns':st.st_mtime_ns,'sha256':sha(p)})
   for batch in pq.ParquetFile(p).iter_batches(batch_size=16,columns=['timestamp','url','api_name','response_data','status_code']):
    for row in batch.to_pylist():
     if str(row['status_code'])!='200' or not str(row['api_name']).endswith('/plan'):continue
     eva=eva_of(row['url'] or '')
     if eva not in evas:continue
     recorded=_to_epoch(row['timestamp']);body=row['response_data']
     if not body or recorded is None:continue
     raw=body.encode() if isinstance(body,str) else body
     for stop in parse_timetable(raw).stops:
      key=(run_key(stop.stop_id),stop.stop_index,eva)
      if key not in targets or not stop.arrival or stop.arrival.planned_ts is None:continue
      if stop.eva is not None and stop.eva!=eva:raise ValueError('payload and URL station differ')
      pt=stop.arrival.planned_ts;r=targets[key];plans[key].add(pt)
      evidence.append({'stop_id':r['id'],'source_run_key':key[0],'stop_sequence':key[1],'eva':eva,'raw_source_stop_id':stop.stop_id,'planned_arrival_utc':pt,'archive_recorded_at':recorded,'before_corrected_five_hour_cutoff':recorded<=pt-18000,'response_sha256':hashlib.sha256(raw).hexdigest(),'partition':str(p.relative_to(ROOT))})
 if not evidence:raise ValueError('no raw-plan provenance recovered')
 with (OUT/'raw-plan-source-evidence.csv').open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(evidence[0]));w.writeheader();w.writerows(evidence)
 before=[];after=[];offset=[];early_available=0
 for key,r in targets.items():
  expected={int(x) for x in r['raw_plan_utc_values'].split(';') if x}
  if plans[key]!=expected:raise ValueError('API-plan provenance differs from saved plan values')
  if len(plans[key])!=1:raise ValueError('plan correction remains ambiguous')
  if r['raw_changed_matches_database_actual']!='True':raise ValueError('changed-time provenance does not match database')
  pt=next(iter(plans[key]));before.append(int(r['arr_delay_s'])/60);after.append((int(r['actual'])-pt)/60);offset.append((int(r['scheduled'])-pt)/60)
  early_available+=any(e['stop_id']==r['id'] and e['before_corrected_five_hour_cutoff'] for e in evidence)
 result={'target_stops':len(rs),'unique_source_plans_verified':len(rs),'all_changed_times_match_database_actual':True,'all_source_plan_sets_reconciled_to_csv':True,'raw_plan_observed_before_corrected_five_hour_cutoff':early_available,'current_mean_target_min':float(np.mean(before)),'proposed_mean_target_min':float(np.mean(after)),'proposed_target_min_range':[float(min(after)),float(max(after))],'proposed_targets_below_minus5':sum(x< -5 for x in after),'schedule_offset_min_range':[float(min(offset)),float(max(offset))],'archive_content_identities':identities,'output_content_identities':{p.name:sha(p) for p in [OUT/'raw-plan-source-evidence.csv',OUT/'raw-early-plan-reconciliation.csv',OUT/'raw-receipt-stop-coverage.csv',OUT/'raw-receipt-result.json',OUT/'proposed-label-reconciliation.csv']},'policy_status':'proposal only; no labels changed','cgroup_peak_bytes':int((cg/'memory.peak').read_text())}
 (OUT/'raw-plan-validation.json').write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k not in ('archive_content_identities','output_content_identities')}),flush=True)
if __name__=='__main__':main()
