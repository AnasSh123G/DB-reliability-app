"""Reuse gates preserve the prior, verified label/unchanged-model evidence."""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_recency_training as controller


def test_reuse_requires_bound_prerequisites_and_rejects_changes(tmp_path):
    files = {}
    for name in controller.PREREQUISITES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert controller.verified_prerequisites({'prerequisite_files': files}, tmp_path) == files
    missing = dict(files)
    missing.pop(next(iter(missing)))
    with pytest.raises(ValueError, match='required'):
        controller.verified_prerequisites({'prerequisite_files': missing}, tmp_path)
    (tmp_path / next(iter(files))).write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        controller.verified_prerequisites({'prerequisite_files': files}, tmp_path)


def test_reuse_refuses_absent_and_escaping_evidence(tmp_path):
    with pytest.raises(ValueError, match='required'):
        controller.verified_prerequisites({}, tmp_path)
    files = {name: 'a' * 64 for name in controller.PREREQUISITES}
    files['../escape'] = 'a' * 64
    with pytest.raises(ValueError, match='repository'):
        controller.verified_prerequisites({'prerequisite_files': files}, tmp_path)
