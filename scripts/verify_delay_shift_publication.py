"""Content identities and normal controls for completed read-only diagnostics."""
from pathlib import Path
import csv,hashlib,json,sqlite3,sys,os,resource,time
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from investigate_delay_shift import QUERY
from explore_compact_delay import guard
OUT=ROOT/'data/ml/delay_shift_audit_20261013'

def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for data in iter(lambda:f.read(2**20),b''):
   h.update(data)
   if hasattr(os,'posix_fadvise'):os.posix_fadvise(f.fileno(),f.tell()-len(data),len(data),os.POSIX_FADV_DONTNEED)
 return h.hexdigest()
def main():
 cg=guard();start=time.monotonic()
 if int((cg/'memory.max').read_text())>2*2**30:raise ValueError('private <=2GiB required')
 manifest=json.loads((OUT/'manifest.json').read_text());db=ROOT/'data/dbrel.sqlite';wal=Path(str(db)+'-wal')
 if wal.exists() and wal.stat().st_size:raise ValueError('nonempty WAL: full source snapshot needs a separate plan')
 c=sqlite3.connect(f'file:{db}?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA cache_size=-32768');c.execute('BEGIN')
 c.execute('SELECT id FROM trip LIMIT 1').fetchone();version=c.execute('PRAGMA data_version').fetchone()[0]
 hashes={}
 paths=[db,Path(manifest['reference']['path'])]+[Path(x['path']) for x in manifest['prepared_files'].values()]
 for p in paths:
  before=p.stat();hashes[str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)]={'sha256':sha(p),'bytes':before.st_size}
  if (p.stat().st_size,p.stat().st_mtime_ns)!=(before.st_size,before.st_mtime_ns):raise ValueError('input changed during hash')
  (OUT/'publication-input-hashes.json').write_text(json.dumps(hashes,indent=2))
  print('Hashed',p.name,flush=True)
 examples=list(csv.DictReader((OUT/'early-label-examples.csv').open()));controls=[]
 # Same-station/date/source/segment comparisons, not used as a second August baseline.
 for x in examples[:24]:
  q=QUERY.replace('FROM trip t CROSS JOIN trip_stop ts ON ts.trip_id=t.id','FROM trip_stop ts CROSS JOIN trip t ON t.id=ts.trip_id')+''' WHERE t.service_date=? AND ts.station_id=? AND t.source=? AND tr.segment=?
   AND ts.quality='RECORDED' AND ts.cancelled=0 AND ts.arr_cancelled=0
   AND ts.actual_arrival_utc IS NOT NULL AND ts.arr_delay_s BETWEEN -300 AND 300 LIMIT 2'''
  for raw in c.execute(q,(x['service_date'],int(x['station_id']),x['source'],x['segment'])):
   r=dict(raw);r['matched_early_example_stop_id']=x['stop_id'];r['derived_delay_min']=(r['actual']-r['scheduled'])/60 if r['scheduled'] is not None else None
   controls.append(r)
 if controls:
  with (OUT/'normal-controls.csv').open('w',newline='') as f:
   w=csv.DictWriter(f,fieldnames=list(controls[0]));w.writeheader();w.writerows(controls)
 # Verify true final run stop separately from the already-labelled last-common metric.
 trips=list(csv.DictReader((OUT/'trip-comparison.csv').open()));stops=list(csv.DictReader((OUT/'stop-comparison.csv').open()));lookup={(int(r['train_id']),int(r['stop_sequence'])):r for r in stops}
 terminals=[]
 for r in trips:
  tid=int(r['september_trip_id']);final=c.execute('SELECT stop_sequence,station_id FROM trip_stop WHERE trip_id=? ORDER BY stop_sequence DESC LIMIT 1',(tid,)).fetchone()
  match=lookup.get((int(r['train_id']),final['stop_sequence'])) if final else None
  terminals.append({'train_id':r['train_id'],'september_trip_id':tid,'terminal_sequence':final['stop_sequence'] if final else None,'terminal_station_id':final['station_id'] if final else None,'terminal_august_supported':bool(match and int(match['august_dates'])>=10),'terminal_deviation_min':match['deviation_min'] if match and int(match['august_dates'])>=10 else None})
 with (OUT/'terminal-comparison.csv').open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(terminals[0]));w.writeheader();w.writerows(terminals)
 after=c.execute('PRAGMA data_version').fetchone()[0];c.rollback()
 if version!=after or (wal.exists() and wal.stat().st_size):raise ValueError('database version/WAL changed')
 for p in [OUT/'matched-delays.f32',OUT/'matched-status.u8',OUT/'trip-comparison.csv',OUT/'stop-comparison.csv']:
  hashes[str(p.relative_to(ROOT))]={'sha256':sha(p),'bytes':p.stat().st_size}
 result={'content_identities':hashes,'normal_control_rows':len(controls),'controls_timestamp_consistent':all(r['derived_delay_min'] is not None and abs(r['derived_delay_min']-r['delay_s']/60)<1e-4 for r in controls),'supported_true_terminals':sum(x['terminal_august_supported'] for x in terminals),'database_read_snapshot':True,'publication_data_version':version,'original_streaming_provenance':'Initial run used source stat identities, not a transaction; these content hashes identify publication inputs. They do not retroactively prove absence of transient edits during the original run.','resources':{'seconds':time.monotonic()-start,'cgroup_peak_bytes':int((cg/'memory.peak').read_text())}}
 (OUT/'publication-verification.json').write_text(json.dumps(result,indent=2));print('Publication controls and fingerprints complete',flush=True)
if __name__=='__main__':main()
