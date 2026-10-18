"""Small synthetic checks for the correction/scoring publication boundary."""
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import verify_correction_version as verifier
from score_label_correction_baselines import Score, shown


def dump(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def corrected(tmp_path, monkeypatch):
    monkeypatch.setattr(verifier, 'OUT', tmp_path)
    monkeypatch.setattr(verifier, 'SOURCE', tmp_path/'original')
    (tmp_path/'evidence').mkdir()
    progress = {}
    for role, date in [('fit', '2026-08-21'), ('eval', '2026-09-15')]:
        directory = tmp_path/'prepared'/f'{verifier.PREFIX}{role}'
        directory.mkdir(parents=True)
        day = np.full(2, int(np.datetime64(date, 'D').astype(int)), dtype='<i4')
        arrays = {'y': np.array([0, 6], dtype='<f4'), 'status': np.array([1, 2], dtype='u1'),
                  'scheduled': np.array([100, 200], dtype='<i8')}
        day.tofile(directory/'day.bin')
        for name, filename in [('y', 'y.bin'), ('status', 'label_status.u8'), ('scheduled', 'target_scheduled_utc.i64')]:
            arrays[name].tofile(directory/filename)
        (tmp_path/'evidence'/f'{date}.csv').write_text('example evidence\n')
        progress[date] = {'role': role, 'rows': 2,
                          'blocks_sha256': {name: hashlib.sha256(a.tobytes()).hexdigest() for name, a in arrays.items()},
                          'evidence_sha256': verifier.file_hash(tmp_path/'evidence'/f'{date}.csv')}
    source = {'columns': {}, 'metadata': {}}
    for role in ('fit', 'eval'):
        directory = tmp_path/'prepared'/f'{verifier.PREFIX}{role}'
        original = verifier.SOURCE/f'{verifier.PREFIX}{role}'; original.mkdir(parents=True)
        for p in directory.glob('*.bin'): (original/p.name).write_bytes(p.read_bytes())
        dump(original/'meta.json', {'n':2})
        source['columns'][role] = {p.name: {'bytes':p.stat().st_size, 'sha256':verifier.file_hash(p)} for p in original.glob('*.bin')}
        source['metadata'][role] = {'meta.json':verifier.file_hash(original/'meta.json')}
    dump(tmp_path/'source-inputs.json', source)
    dump(tmp_path/'setup.json', {'source_identity_sha':verifier.file_hash(tmp_path/'source-inputs.json')})
    dump(tmp_path/'progress.json', progress)
    dump(tmp_path/'result.json', {'dates': 2, 'policy': {'version': 'test'}})
    for role in ('fit', 'eval'):
        directory = tmp_path/'prepared'/f'{verifier.PREFIX}{role}'
        record = {'status': 'complete', 'setup_sha': verifier.file_hash(tmp_path/'setup.json'),
                  'progress_sha': verifier.file_hash(tmp_path/'progress.json'), 'policy': {'version': 'test'},
                  'columns': {p.name: {'bytes': p.stat().st_size, 'sha256': verifier.file_hash(p)}
                              for p in directory.glob('*.bin')}}
        dump(directory/'key.json', {'label_correction': record})
        dump(directory/'meta.json', {'n': 2, 'label_correction': record})
    return tmp_path


def test_committed_correction_accepts_complete_snapshot(corrected):
    assert verifier.verify()['dates'] == 2


def test_status_permutation_cannot_relabel_verified_cohort(corrected):
    path = corrected/'prepared'/f'{verifier.PREFIX}eval'/'label_status.u8'
    np.array([2, 1], dtype='u1').tofile(path)
    with pytest.raises(ValueError, match='committed correction block differs'):
        verifier.verify()


def test_evidence_replacement_is_rejected(corrected):
    (corrected/'evidence/2026-09-15.csv').write_text('replacement\n')
    with pytest.raises(ValueError, match='correction evidence differs'):
        verifier.verify()


def test_original_and_corrected_labels_are_scored_independently():
    original, corrected = Score(), Score()
    point = np.array([0., 2., np.nan])
    original.add(np.array([-60., 7., 0.]), point)
    corrected.add(np.array([10., 7., 0.]), point)
    assert original.result()['mae_scored'] == 32.5
    assert corrected.result()['mae_scored'] == 7.5
    assert corrected.result()['within_5_min'] == 1/3
    assert corrected.result()['missing'] == 1
    np.testing.assert_equal(shown(np.array([-1., .999, 1., 1.5, np.nan])), [0, 0, 1, 2, np.nan])
