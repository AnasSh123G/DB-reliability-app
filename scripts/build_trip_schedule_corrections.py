"""Versioned target-only dated-plan corrections; original facts and inputs remain intact.

Uses the original eligible row population. Late-received plans correct the
retrospective target reference only, never forecast-time features. No ambiguous
or unsupported source plan replaces a label. Streams one service date at a time.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,os,resource,shutil,sqlite3,sys,time
from collections import Counter
from datetime import date,timedelta
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from dbrel.ingest.archive import ArchiveStore,list_day,download,_to_epoch,eva_of,covered_hours
from dbrel.ingest.parser import parse_timetable,run_key
from explore_compact_delay import guard
SOURCE=ROOT/'data/ml/full_year/prepared'
PREFIX='search-2026-09-26-robust-no-doy-mc20-rd56-'
OUT=ROOT/'data/ml/trip_schedule_correction_20261013'
PRE=OUT/'prepared'
POLICY={'version':2,'label_reference':'unique raw per-run station-sequence official plan with matching raw changed timestamp',
 'features':'original feature encoding unchanged; late-observed raw plans never inserted into inputs',
 'population':'same original prepared stop IDs, dates and rows; no silent filtering or target clipping',
 'ambiguous_or_unsupported':'retain original target and mark unresolved',
 'outside_previous_120min_bound':'retain true corrected value and report separately; classifier endpoint policy unchanged',
 'status_codes':{'0':'unverified retained original','1':'source-verified original plan','2':'source-verified corrected dated plan','3':'ambiguous plan retained original','4':'unique plan without matching post-event fchg timestamp retained original','5':'incomplete declared archive coverage retained original'}}

def atomic(path,value):
 p=path.with_suffix(path.suffix+'.tmp');p.write_text(json.dumps(value,indent=2));p.replace(path)
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(2**20),b''):
   h.update(b)
   if hasattr(os,'posix_fadvise'):os.posix_fadvise(f.fileno(),f.tell()-len(b),len(b),os.POSIX_FADV_DONTNEED)
 return h.hexdigest()
def copy_bounded(src,dst):
 if dst.exists():return
 part=dst.with_suffix(dst.suffix+'.part');h=hashlib.sha256()
 with src.open('rb') as r,part.open('wb') as w:
  for b in iter(lambda:r.read(2**20),b''):
   h.update(b);w.write(b)
   if hasattr(os,'posix_fadvise'):os.posix_fadvise(r.fileno(),r.tell()-len(b),len(b),os.POSIX_FADV_DONTNEED)
  w.flush();os.fsync(w.fileno())
 if part.stat().st_size!=src.stat().st_size:raise ValueError('incomplete bounded copy')
 part.replace(dst)
 return h.hexdigest()
def available_memory():
 return int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))*1024

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--pilot',action='store_true');args=ap.parse_args()
 cg=guard()
 if int((cg/'memory.max').read_text())>2*2**30:raise ValueError('private<=2GiB zero-swap cap required')
 if available_memory()<7*2**30:raise ValueError('insufficient memory headroom')
 OUT.mkdir(parents=True,exist_ok=True);PRE.mkdir(exist_ok=True)
 if (OUT/'result.json').exists():
  print('Correction version already completed; no writes',flush=True);return
 c=sqlite3.connect(f'file:{ROOT/"data/dbrel.sqlite"}?mode=ro',uri=True);c.execute('PRAGMA cache_size=-32768');c.execute('PRAGMA temp_store=FILE')
 wal=Path(str(ROOT/'data/dbrel.sqlite')+'-wal')
 if wal.exists() and wal.stat().st_size:raise ValueError('source WAL not empty; freeze a source snapshot first')
 c.execute('BEGIN');initial_version=c.execute('PRAGMA data_version').fetchone()[0];c.execute('SELECT id FROM trip LIMIT 1').fetchone()
 source_identity={'database':{'sha256':sha(ROOT/'data/dbrel.sqlite'),'bytes':(ROOT/'data/dbrel.sqlite').stat().st_size},'columns':{},'metadata':{}}
 for role in ('fit','eval'):
  directory=SOURCE/f'{PREFIX}{role}'
  source_identity['metadata'][role]={name:sha(directory/name) for name in ('meta.json','key.json')}
  source_identity['columns'][role]={p.name:{'sha256':sha(p),'bytes':p.stat().st_size} for p in directory.glob('*.bin')}
 if (OUT/'source-inputs.json').exists() and json.loads((OUT/'source-inputs.json').read_text())!=source_identity:
  raise ValueError('original source content changed; use a new version')
 atomic(OUT/'source-inputs.json',source_identity)
 print('Frozen original content identities',flush=True)
 current={'policy':POLICY,'script_sha':sha(Path(__file__)),'source_keys':{r:sha(SOURCE/f'{PREFIX}{r}/key.json') for r in ('fit','eval')},'source_identity_sha':sha(OUT/'source-inputs.json'),'dependencies':{name:sha(ROOT/name) for name in ('src/dbrel/ingest/parser.py','src/dbrel/ingest/archive.py')}}
 if (OUT/'setup.json').exists() and json.loads((OUT/'setup.json').read_text())!=current:raise ValueError('setup changed; preserve version and choose a new correction root')
 atomic(OUT/'setup.json',current)
 roles={};all_dates=[]
 for role in ('fit','eval'):
  src=SOURCE/f'{PREFIX}{role}';dst=PRE/f'{PREFIX}{role}';dst.mkdir(exist_ok=True)
  original_meta=json.loads((src/'meta.json').read_text());key=json.loads((src/'key.json').read_text());n=original_meta['n']
  # No changes to vocab/scaling or original features. Every new binary is local.
  for p in src.glob('*.bin'):
   if shutil.disk_usage(ROOT).free < 35*2**30:raise ValueError('disk reserve below35GiB')
   copy_bounded(p,dst/p.name)
   if p.name!='y.bin' and sha(dst/p.name)!=source_identity['columns'][role][p.name]['sha256']:
    raise ValueError('copied feature/identity content differs from frozen source')
  days=np.memmap(src/'day.bin',dtype='<i4',mode='r',shape=(n,));ids=np.memmap(src/'stop_id.bin',dtype='<i8',mode='r',shape=(n,));oldy=np.memmap(src/'y.bin',dtype='<f4',mode='r',shape=(n,));y=np.memmap(dst/'y.bin',dtype='<f4',mode='r+',shape=(n,))
  for filename,dtype in [('label_status.u8','u1'),('target_scheduled_utc.i64','<i8')]:
   p=dst/filename
   if not p.exists():
    with p.open('wb') as f:f.truncate(n*np.dtype(dtype).itemsize)
  status=np.memmap(dst/'label_status.u8',dtype='u1',mode='r+',shape=(n,));scheduled=np.memmap(dst/'target_scheduled_utc.i64',dtype='<i8',mode='r+',shape=(n,))
  first=int(np.datetime64(key['rows']['first'],'D').astype(int));last=int(np.datetime64(key['rows']['last'],'D').astype(int))
  role_dates=[d for d in range(first,last+1) if np.searchsorted(days,d)<np.searchsorted(days,d,side='right')];all_dates.extend((str(np.datetime64(int(d),'D')),role,int(d)) for d in role_dates)
  roles[role]={'src':src,'dst':dst,'meta':original_meta,'key':key,'n':n,'day':days,'ids':ids,'oldy':oldy,'y':y,'status':status,'scheduled':scheduled}
  atomic(dst/'meta.json',{**original_meta,'label_correction':{'status':'building','policy':POLICY}})
  atomic(dst/'key.json',{**key,'label_correction':{'status':'building','setup_sha':sha(OUT/'setup.json')}})
  print('Prepared local version',role,n,flush=True)
 eva_map=dict(c.execute('SELECT id,eva FROM station'))
 store=ArchiveStore(ROOT/'data/archive');remote_cache={};stats=json.loads((OUT/'progress.json').read_text()) if (OUT/'progress.json').exists() else {};proof_csv=OUT/'correction-evidence.csv';download_records=json.loads((OUT/'archive-acquisition.json').read_text()) if (OUT/'archive-acquisition.json').exists() else {}
 wanted={'2025-11-03','2026-08-21','2026-09-15'} if args.pilot else None
 for day,role,epoch in sorted(all_dates):
  if wanted is not None and day not in wanted:continue
  if day in stats:
   r=roles[role];a=int(np.searchsorted(r['day'],epoch));b=int(np.searchsorted(r['day'],epoch,side='right'))
   for name,arr in [('y',r['y']),('status',r['status']),('scheduled',r['scheduled'])]:
    if hashlib.sha256(np.asarray(arr[a:b]).tobytes()).hexdigest()!=stats[day]['blocks_sha256'][name]:raise ValueError('committed correction block changed')
   if sha(OUT/'evidence'/f'{day}.csv')!=stats[day]['evidence_sha256']:raise ValueError('committed evidence changed')
   continue
  r=roles[role];a=int(np.searchsorted(r['day'],epoch));b=int(np.searchsorted(r['day'],epoch,side='right'))
  r['y'][a:b]=r['oldy'][a:b];r['status'][a:b]=0;r['scheduled'][a:b]=0
  idmap={int(k):a+i for i,k in enumerate(r['ids'][a:b])};targets={};date_stats=Counter();started=time.monotonic()
  q='''SELECT ts.id,t.source_run_key,ts.stop_sequence,ts.station_id,ts.supposed_arrival_utc,ts.actual_arrival_utc,t.source,ts.arr_delay_s FROM trip t CROSS JOIN trip_stop ts ON ts.trip_id=t.id WHERE t.service_date=?'''
  for sid,source_key,seq,station,old_schedule,actual,source,delay_s in c.execute(q,(day,)):
   if sid not in idmap:continue
   if abs(float(r['oldy'][idmap[sid]])-delay_s/60)>1e-4:raise ValueError('original labels differ from frozen source database')
   date_stats['source_prepared_rows']+=1
   if source!='tts' or actual is None or old_schedule is None or eva_map.get(station) is None:continue
   key=(source_key,seq,eva_map[station]);value=(idmap[sid],sid,old_schedule,actual)
   if key in targets:raise ValueError('source stop identity ambiguous')
   targets[key]=value
  if date_stats['source_prepared_rows']!=b-a:raise ValueError('prepared source row missing')
  plans={};matched_changes={};receipt={};seen_parts=[];target_evas={k[2] for k in targets}
  d=date.fromisoformat(day)
  all_parts=[]
  for edge in (-1,0,1):
   fetchday=d+timedelta(days=edge)
   if fetchday not in remote_cache:
    remotes=list_day(fetchday);remote_cache[fetchday]=remotes
    for remote in remotes:
     if shutil.disk_usage(ROOT).free-remote.size<35*2**30:raise ValueError('archive download would breach35GiB disk reserve')
     if not store.is_intact(remote):download(remote,store)
     download_records[remote.path]={'bytes':remote.size,'oid':remote.oid}
    store.save();atomic(OUT/'archive-acquisition.json',download_records)
   all_parts.extend(remote_cache[fetchday])
  covered=covered_hours([store.local_path(x) for x in all_parts])
  required={(d,h) for h in range(24)}|{(d+timedelta(days=1),h) for h in range(24)}|{(d-timedelta(days=1),h) for h in range(20,24)}
  missing=sorted((str(day),hour) for day,hour in required-covered)
  if missing:
   r['status'][a:b]=5
   date_stats['incomplete_archive_rows']=b-a
   print(day,'missing declared archive coverage',missing,flush=True)
  for remote in ([] if missing else all_parts):
   path=store.local_path(remote);st=path.stat();part_sha=sha(path);seen_parts.append({'path':str(path.relative_to(ROOT)),'sha256':part_sha,'bytes':st.st_size,'mtime_ns':st.st_mtime_ns})
   if len(remote.oid)==64 and part_sha!=remote.oid:raise ValueError('archive content differs from upstream SHA256')
   for batch in pq.ParquetFile(path).iter_batches(batch_size=16,columns=['timestamp','url','api_name','response_data','status_code']):
    for row in batch.to_pylist():
     if str(row['status_code'])!='200':continue
     eva=eva_of(row['url'] or '')
     if eva not in target_evas:continue
     body=row['response_data'];recorded=_to_epoch(row['timestamp'])
     if not body or recorded is None:continue
     raw=body.encode() if isinstance(body,str) else body
     parsed=parse_timetable(raw)
     for stop in parsed.stops:
      key=(run_key(stop.stop_id),stop.stop_index,eva)
      if key not in targets or not stop.arrival:continue
      if stop.eva is not None and stop.eva!=eva:raise ValueError('payload station mismatch')
      event=stop.arrival
      if event.planned_ts is not None and str(row['api_name']).endswith('/plan'):
       plans.setdefault(key,set()).add(event.planned_ts)
       signature=(key,event.planned_ts)
       if signature not in receipt or recorded<receipt[signature]['recorded_at']:
        receipt[signature]={'recorded_at':recorded,'response_sha256':hashlib.sha256(raw).hexdigest(),'partition':str(path.relative_to(ROOT))}
      if str(row['api_name']).endswith('/fchg') and event.changed_ts==targets[key][3] and recorded>=event.changed_ts+300:
       if key not in matched_changes or recorded<matched_changes[key]['changed_recorded_at']:
        matched_changes[key]={'changed_recorded_at':recorded,'changed_response_sha256':hashlib.sha256(raw).hexdigest(),'changed_partition':str(path.relative_to(ROOT)),'changed_endpoint':str(row['api_name'])}
   if (path.stat().st_size,path.stat().st_mtime_ns)!=(st.st_size,st.st_mtime_ns) or sha(path)!=part_sha:raise ValueError('archive changed during read')
  evidence=[]
  for key,(index,sid,old_schedule,actual) in ([] if missing else targets.items()):
   pp=plans.get(key,set());date_stats['target_tts_rows']+=1
   if not pp:date_stats['missing_plan']+=1;continue
   if len(pp)!=1:r['status'][index]=3;date_stats['ambiguous_plan']+=1;continue
   if key not in matched_changes:r['status'][index]=4;date_stats['raw_changed_not_matched']+=1;continue
   pt=next(iter(pp));new=(actual-pt)/60
   if not np.isfinite(new):raise ValueError('nonfinite source correction')
   r['scheduled'][index]=pt;r['y'][index]=new;r['status'][index]=1 if pt==old_schedule else 2
   date_stats['source_verified']+=1;date_stats['corrected']+=pt!=old_schedule;date_stats['corrected_negative_to_nonnegative']+=float(r['oldy'][index])< -5 and new>=-5
   date_stats['corrected_outside_previous_120min_bound']+=abs(new)>120
   rec=receipt[(key,pt)];date_stats['plan_seen_before_corrected_5h_cutoff']+=rec['recorded_at']<=pt-18000
   if pt!=old_schedule:
    evidence.append({'date':day,'role':role,'prepared_row':index,'stop_id':sid,'source_run_key':key[0],'stop_sequence':key[1],'eva':key[2],'original_target_min':float(r['oldy'][index]),'corrected_target_min':new,'original_schedule_utc':old_schedule,'dated_plan_utc':pt,'actual_utc':actual,**rec,**matched_changes[key]})
  for arr in (r['y'],r['status'],r['scheduled']):arr.flush()
  for filename in ('y.bin','label_status.u8','target_scheduled_utc.i64'):
   fd=os.open(r['dst']/filename,os.O_RDONLY);os.fsync(fd);os.close(fd)
  proof_dir=OUT/'evidence';proof_dir.mkdir(exist_ok=True)
  proof_file=proof_dir/f'{day}.csv';proof_temp=proof_file.with_suffix('.csv.tmp')
  with proof_temp.open('w',newline='') as f:
   if evidence:
    w=csv.DictWriter(f,fieldnames=list(evidence[0]));w.writeheader();w.writerows(evidence)
   f.flush();os.fsync(f.fileno())
  proof_temp.replace(proof_file)
  stats[day]={'role':role,'rows':b-a,'counts':dict(date_stats),'archive_inputs':seen_parts,'missing_coverage':missing,'evidence_sha256':sha(proof_file),
   'blocks_sha256':{name:hashlib.sha256(np.asarray(arr[a:b]).tobytes()).hexdigest() for name,arr in [('y',r['y']),('status',r['status']),('scheduled',r['scheduled'])]},'seconds':time.monotonic()-started}
  atomic(OUT/'progress.json',stats);print(day,json.dumps(dict(date_stats)),'seconds',round(stats[day]['seconds'],1),flush=True)
  del idmap,targets,plans,matched_changes,receipt,evidence
  # Allow consumed pages to be reclaimed under the private cap.
  remote_cache = {k:v for k,v in remote_cache.items() if k >= d-timedelta(days=1)}
 c.rollback()
 if c.execute('PRAGMA data_version').fetchone()[0]!=initial_version:raise ValueError('database changed during correction snapshot; committed dates retained for review')
 c.close()
 if not args.pilot and len(stats)==len(all_dates):
  for role,r in roles.items():
   columns={p.name:{'bytes':p.stat().st_size,'sha256':sha(p)} for p in r['dst'].glob('*.bin')}
   label_record={'status':'complete','actual_dates':{'first':str(np.datetime64(int(r['day'][0]),'D')),'last':str(np.datetime64(int(r['day'][-1]),'D'))},'policy':POLICY,'setup_sha':sha(OUT/'setup.json'),'progress_sha':sha(OUT/'progress.json'),'columns':columns}
   atomic(r['dst']/'meta.json',{**r['meta'],'label_correction':label_record})
   atomic(r['dst']/'key.json',{**r['key'],'label_correction':label_record})
  atomic(OUT/'result.json',{'dates':len(stats),'policy':POLICY,'prepared_root':str(PRE.relative_to(ROOT)),'peak_memory_bytes':int((cg/'memory.peak').read_text())})
 else:atomic(OUT/'pilot-result.json',{'processed_dates':list(stats),'policy':POLICY,'peak_memory_bytes':int((cg/'memory.peak').read_text())})
 print('Correction pass complete',flush=True)
if __name__=='__main__':main()
