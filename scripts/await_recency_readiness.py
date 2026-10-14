"""Continue the authorized comparison after correction and review finish."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT/'data/ml/recency_training_20261013'
UNIT = 'dbrel-schedule-correction-full-20261013.service'


def main():
    cg = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    cap = Path('/sys/fs/cgroup')/cg.lstrip('/')
    if (cap/'memory.max').read_text().strip() == 'max' or int((cap/'memory.max').read_text()) > 2**30 \
            or (cap/'memory.swap.max').read_text().strip() != '0':
        raise SystemExit('supervisor requires a private <=1GiB zero-swap cap')
    deadline = time.monotonic()+24*3600
    previous = None
    while time.monotonic() < deadline:
        correction = (ROOT/'data/ml/trip_schedule_correction_20261013/result.json').exists()
        readiness = (WORK/'readiness.json').exists()
        state = subprocess.run(['systemctl','--user','show',UNIT,'--property=ActiveState','--value'],
                               capture_output=True,text=True).stdout.strip()
        active = state in ('active','activating','deactivating')
        if not active and not correction:
            raise SystemExit('correction stopped before completion; fitting refused, progress retained')
        if correction and readiness and not active:
            print('Correction complete and review marker present; starting the serial comparison',flush=True)
            code = subprocess.run([str(ROOT/'.venv/bin/python'),'-u','scripts/run_recency_training.py'],cwd=ROOT).returncode
            raise SystemExit(code)
        status = 'waiting for full correction' if active else 'waiting for final source review'
        if status != previous:
            print(status,flush=True); previous=status
        time.sleep(60)
    raise SystemExit('supervisor deadline reached; progress retained and fitting not started')


if __name__ == '__main__': main()
