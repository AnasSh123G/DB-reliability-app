from pathlib import Path
from datetime import date
import json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from dbrel.ingest.archive import list_day
out=[]
for d in (date(2025,11,3),date(2026,8,22)):
 try:
  fs=list_day(d);row={'date':str(d),'files':len(fs),'bytes':sum(f.size for f in fs),'partitions':[{'path':f.path,'bytes':f.size,'oid':f.oid} for f in fs]}
 except Exception as e:row={'date':str(d),'error':str(e),'kind':type(e).__name__}
 out.append(row);print(json.dumps(row),flush=True)
(ROOT/'data/ml/trip_schedule_correction_20261013/archive-probe.json').write_text(json.dumps(out,indent=2))
