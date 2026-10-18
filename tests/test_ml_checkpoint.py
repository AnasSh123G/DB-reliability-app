"""Checkpoints are only reused for the exact same setup, and half-written files never count."""

from __future__ import annotations

import json

import numpy as np
import pytest

from dbrel.ml.checkpoint import CheckpointMismatch, LibraryCheckpoint, checkpoint_key

META = {"built_at": 100.0, "db_fingerprint": [1, 2, 3], "n_rows": 10}
SPLIT = {"train": ["2026-09-01", "2026-09-02"], "validation": ["2026-09-03"], "test": ["2026-09-04"]}


def key(**changes):
    k = checkpoint_key(META, SPLIT, "catboost", sample_rows=1000, max_rounds=50)
    k.update(changes)
    return k


def test_same_setup_reopens(tmp_path):
    LibraryCheckpoint(tmp_path, "catboost", key()).open()
    LibraryCheckpoint(tmp_path, "catboost", key()).open()


@pytest.mark.parametrize("change, named", [
    ({"split": {**SPLIT, "test": ["2026-09-05"]}}, "split"),
    ({"search_space": "0000000000000000"}, "search_space"),
    ({"dataset": {**META, "built_at": 101.0}}, "dataset.built_at"),
    ({"sample_rows": 2000}, "sample_rows"),
])
def test_a_different_setup_is_refused_by_name(tmp_path, change, named):
    LibraryCheckpoint(tmp_path, "catboost", key()).open()
    with pytest.raises(CheckpointMismatch, match=named):
        LibraryCheckpoint(tmp_path, "catboost", key(**change)).open()


class _FakeCandidate:
    def save(self, prefix):
        path = prefix.with_name(prefix.name + ".bin")
        path.write_bytes(b"model")
        return [path.name]

    def describe(self):
        return {"library": "fake", "params": {"a": 1}}


def test_a_side_counts_only_once_its_fit_file_is_written(tmp_path):
    ck = LibraryCheckpoint(tmp_path, "catboost", key())
    ck.open()
    (ck.dir / "dep.bin").write_bytes(b"half")  # model file from a killed save
    (ck.dir / "dep.fit.json.tmp").write_text("{")  # half-written marker
    assert not ck.has_side("dep")

    ck.open()  # a resume clears the leftovers
    assert not list(ck.dir.glob("*.tmp"))

    pred = np.arange(6.0).reshape(2, 3)
    ck.save_side("dep", _FakeCandidate(), pred_test=pred, validation_mean_pinball=1.5,
                 crossed_share_validation=0.1, crossed_share_test=0.2, threads=2)
    assert ck.has_side("dep") and not ck.has_side("arr")
    loaded = ck.load_side("dep")
    assert np.array_equal(loaded["_pred_test"], pred)
    assert loaded["files"] == ["dep.bin"] and loaded["validation_mean_pinball"] == 1.5


def test_tuning_and_done_marks(tmp_path):
    ck = LibraryCheckpoint(tmp_path, "catboost", key())
    ck.open()
    assert not ck.has_tuning() and not ck.is_done()
    ck.save_tuning({"best_params": {"depth": 6}}, device="cpu", threads=14)
    assert ck.load_tuning() == {"tuning": {"best_params": {"depth": 6}}, "device": "cpu", "threads": 14}
    ck.mark_done()
    assert ck.is_done()
    assert json.loads((ck.dir / "key.json").read_text())["library"] == "catboost"
