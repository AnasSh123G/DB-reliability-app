"""Score unchanged original exports against old and dated-corrected targets."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from dbrel.ml.arrival import distribution_point
from dbrel.ml.full_year import PreparedSet
from explore_compact_delay import guard
from recency_provenance import file_hash, signature
from verify_correction_version import verify

PREFIX = 'search-2026-09-26-robust-no-doy-mc20-rd56-'
CORRECTION = ROOT / 'data/ml/trip_schedule_correction_20261013'
OLD = ROOT / 'data/ml/compact_training_20261013'
OUT = ROOT / 'data/ml/recency_training_20261013/correction_baselines'
REFERENCE = ROOT / 'data/ml/full_year/runs/compact-2026-09-26/trials/f1ac12b1742e3a0b/validation.npz'
BLOCK = 8192


class Score:
    def __init__(self):
        self.n = self.scored = 0
        self.error = 0.0
        self.hits = [0, 0, 0]

    def add(self, y, point):
        error = np.abs(np.asarray(y, dtype='float64') - point)
        if not np.isfinite(y).all() or np.isinf(point).any():
            raise ValueError('invalid targets/predictions')
        good = np.isfinite(error)
        self.n += len(y)
        self.scored += int(good.sum())
        self.error += float(error[good].sum())
        for i, minutes in enumerate((1, 3, 5)):
            self.hits[i] += int((error <= minutes).sum())

    def result(self):
        return {'eligible': self.n, 'scored': self.scored, 'missing': self.n-self.scored,
                'coverage': self.scored/self.n if self.n else 0,
                'mae_scored': self.error/self.scored if self.scored else None,
                **{f'within_{m}_min': self.hits[i]/self.n if self.n else None
                   for i, m in enumerate((1, 3, 5))}}


def shown(point):
    return np.where(point < 1, 0, np.floor(point.astype('float64')+0.5))


def main():
    cg = guard()
    if int((cg/'memory.max').read_text()) > 2*2**30:
        raise ValueError('baseline scoring requires <=2GiB cap')
    if not (CORRECTION/'result.json').exists():
        raise ValueError('full correction is not ready')
    verify()
    data = PreparedSet(CORRECTION/'prepared'/f'{PREFIX}eval')
    if data.key['label_correction']['status'] != 'complete':
        raise ValueError('corrected evaluation incomplete')
    original = ROOT/'data/ml/full_year/prepared'/f'{PREFIX}eval'
    # Verify content, not only metadata, before binding predictions to new rows.
    for name, info in data.key['label_correction']['columns'].items():
        if file_hash(data.dir/name) != info['sha256']:
            raise ValueError(f'corrected content changed: {name}')
    old_y = np.memmap(original/'y.bin', mode='r', dtype='<f4', shape=(len(data),))
    frozen_source = json.loads((CORRECTION/'source-inputs.json').read_text())
    if file_hash(original/'y.bin') != frozen_source['columns']['eval']['y.bin']['sha256']:
        raise ValueError('original evaluation targets changed')
    statuses = np.memmap(data.dir/'label_status.u8', mode='r', dtype='u1', shape=(len(data),))
    day = data.column('day')
    # Bounded read for the chronological boundary.
    cutoff = int(np.datetime64('2026-09-06','D').astype(int))
    lo, hi = 0, len(data)
    while lo < hi:
        mid = (lo+hi)//2
        if int(day[mid:mid+1][0]) < cutoff: lo = mid+1
        else: hi = mid
    start = lo
    if not 0 < start < len(data):
        raise ValueError('later validation absent')
    OUT.mkdir(parents=True, exist_ok=True)
    inputs = json.loads((OLD/'runs/fine/inputs.json').read_text())
    if any(signature(inputs[k]) != data.key[k] for k in ('prep', 'vocab')):
        raise ValueError('original encoding differs from corrected prepared encoding')
    graph_names = {'fine': 'median', 'distribution': 'window3', 'discrete': 'median'}
    key = {'script_sha': file_hash(Path(__file__)), 'correction_result_sha': file_hash(CORRECTION/'result.json'),
           'prepared_key': data.key, 'reference_sha': file_hash(REFERENCE),
           'status_sha': file_hash(data.dir/'label_status.u8'),
           'graphs': {name: {'onnx_sha': file_hash(OLD/'runs'/name/'onnx/model.onnx'),
                             'prep_sha': file_hash(OLD/'runs'/name/'onnx/prep.json')}
                      for name in graph_names}}
    if (OUT/'key.json').exists() and json.loads((OUT/'key.json').read_text()) != key:
        raise ValueError('baseline scoring provenance changed')
    (OUT/'key.json').write_text(json.dumps(key, indent=2))
    options = ort.SessionOptions(); options.intra_op_num_threads = 2; options.inter_op_num_threads = 1
    stats = {}
    for name in ['reference', *graph_names]:
        scores = {label: {view: Score() for view in ('raw', 'displayed')}
                  for label in ('original', 'corrected', 'corrected_verified', 'corrected_unresolved')}
        points = np.lib.format.open_memmap(OUT/f'{name}.npy', mode='w+', dtype='<f4', shape=(len(data)-start,))
        if name == 'reference':
            with np.load(REFERENCE) as z:
                ids = z['stop_id']; base = z['point']
            if len(ids) != len(data): raise ValueError('reference count differs')
            for a in range(0, len(data), BLOCK):
                if not np.array_equal(ids[a:a+BLOCK], data.column('stop_id')[a:a+BLOCK]):
                    raise ValueError('reference ordered IDs differ')
            session = None
        else:
            prep = json.loads((OLD/'runs'/name/'onnx/prep.json').read_text())
            session = ort.InferenceSession(str(OLD/'runs'/name/'onnx/model.onnx'), options,
                                           providers=['CPUExecutionProvider'])
        for a in range(start, len(data), BLOCK):
            b = min(a+BLOCK, len(data))
            if session is None: point = base[a:b].astype('float32')
            else:
                raw = session.run(None, {'cat': data.cats[a:b].astype('int64'),
                                         'num': data.nums[a:b].astype('float32')})[0]
                if prep['output'] == 'probs':
                    point, _ = distribution_point(raw, np.asarray(prep['edges']), 'window', 3)
                else: point = raw[:, 1]
                point = point.astype('float32')
            if not np.isfinite(point).all(): raise ValueError('nonfinite original export')
            points[a-start:b-start] = point
            verified = np.isin(statuses[a:b], [1, 2])
            for label, target, mask in (
                ('original', old_y[a:b], np.ones(b-a, dtype=bool)),
                ('corrected', data.y[a:b], np.ones(b-a, dtype=bool)),
                ('corrected_verified', data.y[a:b], verified),
                ('corrected_unresolved', data.y[a:b], ~verified)):
                scores[label]['raw'].add(target[mask], point[mask])
                scores[label]['displayed'].add(target[mask], shown(point[mask]))
        points.flush(); del points
        stats[name] = {label: {view: score.result() for view, score in views.items()}
                       for label, views in scores.items()}
        if name == 'reference': del ids, base
        else: del session
        print(name, json.dumps(stats[name]), flush=True)
    ids_out = np.lib.format.open_memmap(OUT/'stop_ids.npy', mode='w+', dtype='<i8', shape=(len(data)-start,))
    for a in range(start, len(data), BLOCK): ids_out[a-start:min(a+BLOCK,len(data))-start] = data.column('stop_id')[a:min(a+BLOCK,len(data))]
    ids_out.flush(); del ids_out
    result = {'scope': 'unchanged original exports on reused later validation; only targets change',
              'scores': stats, 'rows': len(data)-start,
              'arrays': {p.name: file_hash(p) for p in OUT.glob('*.npy')},
              'key_sha': file_hash(OUT/'key.json')}
    temporary = OUT/'result.json.tmp'; temporary.write_text(json.dumps(result, indent=2)); temporary.replace(OUT/'result.json')


if __name__ == '__main__': main()
