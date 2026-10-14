"""Verify full prepared content and committed dated-label evidence before use."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from dbrel.ml.full_year import PreparedSet
from recency_provenance import file_hash

OUT = ROOT/'data/ml/trip_schedule_correction_20261013'
SOURCE = ROOT/'data/ml/full_year/prepared'
PREFIX = 'search-2026-09-26-robust-no-doy-mc20-rd56-'


def verify():
    result = json.loads((OUT/'result.json').read_text())
    progress = json.loads((OUT/'progress.json').read_text())
    setup_record = json.loads((OUT/'setup.json').read_text())
    if file_hash(OUT/'source-inputs.json') != setup_record['source_identity_sha']:
        raise ValueError('original source manifest differs from correction setup')
    source = json.loads((OUT/'source-inputs.json').read_text())
    for role in ('fit', 'eval'):
        original = SOURCE/f'{PREFIX}{role}'
        for name, expected in source['columns'][role].items():
            path = original/name
            if {'bytes':path.stat().st_size, 'sha256':file_hash(path)} != expected:
                raise ValueError(f'original source column differs: {role}/{name}')
        for name, expected in source['metadata'][role].items():
            if file_hash(original/name) != expected:
                raise ValueError(f'original source metadata differs: {role}/{name}')
    setup = file_hash(OUT/'setup.json'); progress_sha = file_hash(OUT/'progress.json')
    if len(progress) != result['dates']:
        raise ValueError('correction date count differs')
    dates = set()
    for role in ('fit', 'eval'):
        data = PreparedSet(OUT/'prepared'/f'{PREFIX}{role}')
        record = data.key['label_correction']
        if (record != data.meta['label_correction'] or record['status'] != 'complete'
                or record['setup_sha'] != setup or record['progress_sha'] != progress_sha
                or record['policy'] != result['policy']):
            raise ValueError('correction identity differs')
        for name, expected in record['columns'].items():
            path = data.dir/name
            if {'bytes': path.stat().st_size, 'sha256': file_hash(path)} != expected:
                raise ValueError(f'corrected column differs: {role}/{name}')
            if name != 'y.bin' and expected != source['columns'][role][name]:
                raise ValueError(f'copied feature/identity differs from original: {role}/{name}')
        day = np.memmap(data.dir/'day.bin', dtype='<i4', mode='r', shape=(len(data),))
        arrays = {'y': np.memmap(data.dir/'y.bin', dtype='<f4', mode='r', shape=(len(data),)),
                  'status': np.memmap(data.dir/'label_status.u8', dtype='u1', mode='r', shape=(len(data),)),
                  'scheduled': np.memmap(data.dir/'target_scheduled_utc.i64', dtype='<i8', mode='r', shape=(len(data),))}
        for date, saved in progress.items():
            if saved['role'] != role: continue
            epoch = int(np.datetime64(date, 'D').astype(int))
            a = int(np.searchsorted(day, epoch)); b = int(np.searchsorted(day, epoch, side='right'))
            if b-a != saved['rows'] or b == a:
                raise ValueError(f'correction row/date identity differs: {date}')
            dates.add(date)
            for name, column in arrays.items():
                if hashlib.sha256(np.asarray(column[a:b]).tobytes()).hexdigest() != saved['blocks_sha256'][name]:
                    raise ValueError(f'committed correction block differs: {date}/{name}')
            if file_hash(OUT/'evidence'/f'{date}.csv') != saved['evidence_sha256']:
                raise ValueError(f'correction evidence differs: {date}')
        # Every row must belong to exactly one committed date. No missing days
        # or independently invented status companions are accepted.
        if sum(saved['rows'] for saved in progress.values() if saved['role'] == role) != len(data):
            raise ValueError('correction row coverage incomplete')
    if dates != progress.keys():
        raise ValueError('unmatched committed dates')
    return result


if __name__ == '__main__':
    from explore_compact_delay import guard
    guard()
    print(json.dumps(verify()), flush=True)
