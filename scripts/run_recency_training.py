"""Serial correction-baseline, controlled fits, browser gates, and final report.

Requires the completed correction version and reviewed/CPU-tested source before
starting any fit. Every child runs in its own swap-disabled memory-limited unit.
"""
from __future__ import annotations

import hashlib
import argparse
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from recency_experiment import conditions as experiment_conditions, directory as experiment_directory
WORK = experiment_directory(ROOT)
TORCH = ROOT/'data/build/torch-rocm/venv/bin/python'
LIGHT = ROOT/'.venv/bin/python'
GIB = 2**30
SOURCES = ('scripts/train_recency_delay.py', 'scripts/check_recency_discrete_delay.py',
           'scripts/recency_experiment.py',
           'scripts/browser_parity.py',
           'scripts/recency_weights.py', 'scripts/recency_provenance.py',
           'scripts/verify_correction_version.py',
           'scripts/score_label_correction_baselines.py', 'scripts/run_recency_training.py',
           'scripts/compare_corrected_august.py',
           'scripts/await_recency_readiness.py', 'scripts/resume_schedule_corrections.py',
           'scripts/explore_delay_browser.py', 'scripts/check_recency_browser.py',
           'scripts/summarize_recency_training.py',
           'scripts/explore_compact_delay.py', 'scripts/check_discrete_delay.py',
           'src/dbrel/ml/arrival.py', 'src/dbrel/ml/arrival_nn.py', 'src/dbrel/ml/full_year.py',
           'src/dbrel/ml/features.py', 'src/dbrel/ml/full_year_search.py', 'src/dbrel/ml/notice_rates.py',
           'bahn-buddy-predict/src/lib/tabm.ts',
           'bahn-buddy-predict/node_modules/onnxruntime-web/package.json',
           'bahn-buddy-predict/node_modules/typescript/package.json',
           'scripts/build_trip_schedule_corrections.py', 'src/dbrel/ingest/parser.py', 'src/dbrel/ingest/archive.py')

PREREQUISITES = (
    'data/ml/trip_schedule_correction_20261013/result.json',
    'data/ml/trip_schedule_correction_20261013/progress.json',
    'data/ml/recency_training_20261013/correction_baselines/key.json',
    'data/ml/recency_training_20261013/correction_baselines/result.json',
    'data/ml/recency_training_20261013/corrected_august_comparison/result.json',
)


def verified_prerequisites(readiness, root):
    """Reuse completed pre-fit measurements only with pinned content identities."""
    from recency_provenance import file_hash
    files = readiness.get('prerequisite_files')
    if not isinstance(files, dict) or not set(PREREQUISITES).issubset(files):
        raise ValueError('required completed prerequisite identities are missing')
    root = Path(root).resolve()
    for name, expected in files.items():
        if not isinstance(name, str) or Path(name).is_absolute() or root not in (root/name).resolve().parents:
            raise ValueError('prerequisite evidence must stay inside repository')
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
            raise ValueError('prerequisite content identity is malformed')
    for name, expected in files.items():
        if file_hash(root/name) != expected:
            raise ValueError(f'completed prerequisite changed: {name}')
    return dict(files)


def fingerprints():
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def validated_chromium(readiness):
    from check_recency_browser import browser_identity
    pin = readiness.get('chromium')
    if not isinstance(pin, dict) or set(pin) != {'path', 'sha256', 'version'} \
            or not all(isinstance(value, str) and value for value in pin.values()):
        raise ValueError('readiness must contain a complete pinned Chromium identity')
    if browser_identity(Path(pin['path']), pin['sha256']) != pin:
        raise ValueError('Chromium identity differs from the reviewed readiness pin')
    return dict(pin)


def available():
    return int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                    if line.startswith('MemAvailable:')))*1024


def atomic(path, value):
    part = path.with_suffix('.json.tmp')
    part.write_text(json.dumps(value, indent=2)); part.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-prerequisites', action='store_true')
    args = parser.parse_args()
    os.chdir(ROOT)
    (WORK/'tmp').mkdir(parents=True, exist_ok=True)
    (WORK/'logs').mkdir(exist_ok=True)
    lock = (WORK/'controller.lock').open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('another comparison controller is already active')
    correction = ROOT/'data/ml/trip_schedule_correction_20261013/result.json'
    if not correction.exists():
        raise SystemExit('complete dated-label correction required before fitting')
    readiness = json.loads((WORK/'readiness.json').read_text())
    if readiness['sources'] != fingerprints() or not readiness['cpu_tests_passed'] or not readiness['review_passed']:
        raise SystemExit('review/test readiness differs from the actual source')
    chromium = validated_chromium(readiness)
    runtime = json.loads((ROOT/'bahn-buddy-predict/node_modules/onnxruntime-web/package.json').read_text())
    if runtime['version'] != '1.30.0':
        raise SystemExit('the reviewed browser comparison requires pinned ONNX Runtime Web 1.30.0')
    state_file = WORK/'queue.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    # A controller interruption can leave its separately capped systemd child
    # running. Refuse a second writer to the same checkpoints on resume.
    for saved in state.values():
        if saved.get('unit'):
            active = subprocess.run(['systemctl','--user','show',saved['unit'],
                                     '--property=ActiveState','--value'],capture_output=True,text=True)
            if active.stdout.strip() in ('active','activating','deactivating'):
                raise SystemExit(f'previous bounded job is still active: {saved["unit"]}')
    jobs = [('corrected-august-comparison', 2, LIGHT, ['scripts/compare_corrected_august.py'], None),
            ('correction-baselines', 2, LIGHT, ['scripts/score_label_correction_baselines.py'], None)]
    if args.reuse_prerequisites:
        verified = verified_prerequisites(readiness, ROOT)
        for name, _, _, _, _ in jobs:
            state[name] = {'status': 'complete', 'provenance': 'reused verified completed measurement',
                           'identity_count': len(verified)}
        atomic(state_file, state)
        jobs = []
    # Two underlying fits produce three views per condition. Architectures,
    # seeds, epoch budgets and data are identical across conditions.
    for condition in experiment_conditions(WORK):
        for track in ('fine', 'distribution'):
            jobs.append((f'{condition}-{track}', 8, TORCH,
                         ['scripts/train_recency_delay.py', 'train', '--condition', condition, '--track', track], None))
        discrete = WORK/condition/'runs/discrete/result.json'
        jobs.append((f'{condition}-discrete', 2, TORCH,
                     ['scripts/check_recency_discrete_delay.py', '--condition', condition], discrete))
        for track in ('fine', 'distribution', 'discrete'):
            jobs.append((f'{condition}-{track}-browser', 2, LIGHT,
                         ['scripts/check_recency_browser.py', '--runtime-root', str(ROOT), '--experiment',
                          str(WORK/condition/'runs'/track), '--chromium', chromium['path'],
                          '--chromium-sha256', chromium['sha256']], None))
    report = ['--report', readiness['report']] if readiness.get('report') else []
    jobs.append(('report', 2, TORCH, ['scripts/summarize_recency_training.py', *report], None))
    for name, cap, python, argv, completed in jobs:
        if fingerprints() != readiness['sources']:
            raise SystemExit('reviewed source changed; later jobs refused')
        if available() < (cap+5)*GIB:
            state[name] = {'status': 'deferred', 'reason': 'insufficient RAM headroom'}
            atomic(state_file, state)
            raise SystemExit('insufficient free RAM; resume later without changing checkpoints')
        if not (ROOT/argv[0]).exists():
            raise SystemExit(f'required stage implementation missing: {argv[0]}')
        log = WORK/'logs'/f'{name}-{time.time_ns()}.log'
        unit = f'dbrel-recency-{name}-{time.time_ns()}'
        command = ['systemd-run', '--user', '--wait', '--pipe', f'--unit={unit}',
                   '--slice=background.slice', f'--property=MemoryMax={cap}G',
                   f'--property=MemoryHigh={5 if cap == 8 else 1.5}G',
                   '--property=MemorySwapMax=0', '--property=Nice=10',
                   f'--property=WorkingDirectory={ROOT}', '/usr/bin/env',
                   f'TMPDIR={WORK / "tmp"}', 'PYTHONDONTWRITEBYTECODE=1',
                   f'DBREL_RECENCY_EXPERIMENT={WORK}',
                   'OPENBLAS_NUM_THREADS=2', 'OMP_NUM_THREADS=4', str(python), '-u', *argv]
        state[name] = {'status': 'running', 'log': str(log.relative_to(ROOT)), 'unit': unit}
        atomic(state_file, state)
        print(f'START {name}: {log.relative_to(ROOT)}', flush=True)
        with log.open('w') as output:
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
            try:
                for line in child.stdout:
                    output.write(line); output.flush(); print(line, end='', flush=True)
                code = child.wait()
            finally:
                if child.poll() is None:
                    subprocess.run(['systemctl', '--user', 'stop', unit], check=False)
                    child.terminate()
                    try: child.wait(timeout=10)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
        state[name].update(status='complete' if code == 0 else 'failed', exit_code=code)
        atomic(state_file, state)
        if code != 0:
            raise SystemExit(f'{name} failed; later jobs refused and checkpoints retained')
    print('Controlled recency comparison and all serving/report gates completed', flush=True)


if __name__ == '__main__': main()
