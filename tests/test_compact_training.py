"""CPU-only checks for the offline compact training helpers.

Run with the repository's torch environment:
    PYTHONPATH=src:scripts data/build/torch-rocm/venv/bin/python -m pytest \
        --noconftest --basetemp=data/ml/compact_training_20261013/tmp/pytest \
        tests/test_compact_training.py
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("tabm")
torch.set_num_threads(1)

from dbrel.ml.arrival import fit_input_prep  # noqa: E402
from dbrel.ml.arrival_nn import ArrivalNet, BatchSource, NetConfig  # noqa: E402
from dbrel.ml.features import CATEGORICAL, NUMERIC  # noqa: E402
from dbrel.ml.full_year import FileColumn  # noqa: E402
import train_compact_delay as compact  # noqa: E402


def test_streamed_score_matches_batch_metrics_with_missing_points_and_quantiles():
    y = np.array([0.0, 1.0, 2.0, 4.0, 8.0, 10.0])
    point = np.array([0.5, np.nan, 3.0, 6.0, 7.0, 14.0])
    pct = np.array([
        [-1.0, 0.5, 2.0],
        [0.0, 0.3, 0.5],
        [2.0, 3.0, 4.0],
        [3.0, 5.0, 7.0],
        [6.0, 8.0, 9.0],
        [11.0, 12.0, 13.0],
    ])

    score = compact.Score()
    for start, stop in ((0, 1), (1, 4), (4, 6)):
        score.add(y[start:stop], point[start:stop], pct[start:stop])

    error = np.abs(y - point)
    expected = {
        "eligible": 6,
        "scored": 5,
        "missing": 1,
        "coverage": 5 / 6,
        "quantile_scored": 6,
        "mae_scored": float(error[np.isfinite(error)].mean()),
        "within_1_min": float(np.count_nonzero(error <= 1) / 6),
        "within_3_min": float(np.count_nonzero(error <= 3) / 6),
        "within_5_min": float(np.count_nonzero(error <= 5) / 6),
        "band_coverage": 4 / 6,
        "below_p25": 2 / 6,
        "below_p75": 5 / 6,
    }
    assert score.result() == pytest.approx(expected)

    point_only = compact.Score()
    point_only.add(y, point)
    diagnostics = point_only.result()
    assert diagnostics["quantile_scored"] == 0
    assert diagnostics["band_coverage"] is None
    assert diagnostics["below_p25"] is None
    assert diagnostics["below_p75"] is None

    crossed = pct.copy()
    crossed[2] = [4.0, 2.0, 1.0]
    with pytest.raises(ValueError, match="percentiles"):
        compact.Score().add(y, point, crossed)


def test_precision_constraint_falls_back_to_lowest_mae_then_within_five():
    compliant = {"coverage": 1.0, "mae_scored": 2.0, "within_5_min": 0.50}
    better_accuracy_but_ineligible = {"coverage": 1.0, "mae_scored": 2.1, "within_5_min": 0.99}
    worse_fallback = {"coverage": 1.0, "mae_scored": 2.5, "within_5_min": 1.0}
    best_fallback = {"coverage": 1.0, "mae_scored": 2.2, "within_5_min": 0.2}
    missing = {"coverage": 0.99, "mae_scored": 0.1, "within_5_min": 1.0}

    assert compact.selection_rank(compliant, 2.0) > compact.selection_rank(
        better_accuracy_but_ineligible, 2.0)
    assert compact.selection_rank(best_fallback, 2.0) > compact.selection_rank(worse_fallback, 2.0)
    assert compact.selection_rank(missing, 2.0) < compact.selection_rank(best_fallback, 2.0)


def test_cached_column_reads_bounded_prefix_and_range_as_copies(tmp_path):
    values = np.arange(30, dtype=np.int32).reshape(10, 3)
    path = tmp_path / "column.bin"
    values.tofile(path)
    column = FileColumn(path, "int32", n=10, width=3)

    prefix = compact.CachedColumn(column, stop=4)
    ranged = compact.CachedColumn(column, start=3, stop=8)
    try:
        assert len(prefix) == 4 and prefix.shape == (4, 3)
        assert len(ranged) == 5 and ranged.shape == (5, 3)
        assert np.array_equal(prefix[:], values[:4])
        assert np.array_equal(ranged[1:4], values[4:7])
        assert np.array_equal(ranged[-2:], values[6:8])
        assert ranged[2:2].shape == (0, 3)
        copied = ranged[:]
        copied[0, 0] = -100
        assert np.array_equal(column[:], values)
        with pytest.raises(TypeError, match="contiguous"):
            ranged[::2]
    finally:
        prefix.close()
        ranged.close()

    with pytest.raises(ValueError, match="invalid column range"):
        compact.CachedColumn(column, start=8, stop=11)


def _tiny_prep():
    n = 24
    encoded = {name: np.zeros(n, dtype=np.int32) for name in CATEGORICAL}
    vocab = {name: [0] for name in CATEGORICAL}
    for name in NUMERIC:
        encoded[name] = np.linspace(-1, 1, n, dtype=np.float32)
    return fit_input_prep(encoded, np.ones(n, dtype=bool), vocab)


def _validation_data(directory: Path, cats, nums, y):
    directory.mkdir()
    cols = []
    for name, values in (("cats", cats), ("nums", nums), ("y", y)):
        path = directory / f"{name}.bin"
        values.tofile(path)
        cols.append(FileColumn(path, values.dtype, len(values), values.shape[1] if values.ndim > 1 else 0))
    return SimpleNamespace(cats=cols[0], nums=cols[1], y=cols[2])


def _tiny_net_and_data(seed=17):
    rng = np.random.default_rng(31)
    cats = np.zeros((12, len(CATEGORICAL)), dtype=np.int32)
    prep = _tiny_prep()
    nums = rng.normal(size=(12, len(prep["numeric"]))).astype(np.float32)
    y = (nums[:, 0] * 2.0 + np.linspace(-0.3, 0.3, len(nums))).astype(np.float32)
    cfg = NetConfig(arch="mlp", hidden=8, blocks=1, embedding_max_dim=2, train_emb_dim=2,
                    batch_size=4, epochs=3, patience=3, lr=1e-2, unknown_rate=0.2, seed=seed)
    return ArrivalNet(cfg, prep), cats, nums, y


def _state_equal(left, right):
    assert left.keys() == right.keys()
    for key in left:
        assert torch.equal(left[key], right[key]), key


def test_checkpoint_resume_restores_cpu_training_evolution(tmp_path, monkeypatch):
    torch.set_num_threads(1)
    net_full, cats, nums, y = _tiny_net_and_data()
    validation = _validation_data(tmp_path / "validation", cats, nums, y)
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    key = {"toy": "same prepared data and config"}
    source = BatchSource(cats, nums, y, block_rows=7)
    full_history, full_best = compact.training(net_full, source, validation, len(y), "cpu",
                                               full_dir, key, None)

    net_resumed, cats2, nums2, y2 = _tiny_net_and_data()
    assert np.array_equal(cats, cats2) and np.array_equal(nums, nums2) and np.array_equal(y, y2)
    resume_dir = tmp_path / "resumed"
    resume_dir.mkdir()
    real_log = compact.log
    interrupted = False

    def interrupt_after_saved_epoch(message):
        nonlocal interrupted
        if message.startswith("epoch 1:") and not interrupted:
            interrupted = True
            raise RuntimeError("simulated process interruption")
        real_log(message)

    monkeypatch.setattr(compact, "log", interrupt_after_saved_epoch)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        compact.training(net_resumed, source, validation, len(y), "cpu", resume_dir, key, None)
    assert (resume_dir / "latest.pt").exists()
    assert (resume_dir / "history.json").exists()

    latest_path = resume_dir / "latest.pt"
    first_checkpoint = torch.load(latest_path, map_location="cpu", weights_only=False)
    first_best = resume_dir / first_checkpoint["best_file"]
    first_best_bytes = first_best.read_bytes()
    assert first_checkpoint["epoch"] == 1
    assert first_checkpoint["best_file"] == "epoch-001.pt"

    original_save_torch = compact.save_torch
    original_rank = compact.selection_rank
    rank_calls = 0

    def increasing_rank(score, mae_limit=None):
        nonlocal rank_calls
        rank_calls += 1
        return (10, rank_calls, 0)

    def interrupt_before_latest_commit(path, value):
        if Path(path).name == "latest.pt" and value["epoch"] == 2:
            raise RuntimeError("simulated interruption before latest commit")
        original_save_torch(path, value)

    monkeypatch.setattr(compact, "selection_rank", increasing_rank)
    monkeypatch.setattr(compact, "save_torch", interrupt_before_latest_commit)
    with pytest.raises(RuntimeError, match="before latest commit"):
        compact.training(net_resumed, source, validation, len(y), "cpu", resume_dir, key, None)

    assert (resume_dir / "epoch-002.pt").exists()  # new best file landed before the commit point
    latest_after_crash = torch.load(latest_path, map_location="cpu", weights_only=False)
    assert latest_after_crash["epoch"] == 1
    assert latest_after_crash["best_file"] == "epoch-001.pt"
    assert first_best.read_bytes() == first_best_bytes  # the committed best file stayed immutable

    monkeypatch.setattr(compact, "selection_rank", original_rank)
    monkeypatch.setattr(compact, "save_torch", original_save_torch)
    monkeypatch.setattr(compact, "log", real_log)
    resumed_history, resumed_best = compact.training(net_resumed, source, validation, len(y), "cpu",
                                                     resume_dir, key, None)
    assert resumed_best == full_best
    assert [{k: v for k, v in row.items() if k != "seconds"} for row in resumed_history] == \
        [{k: v for k, v in row.items() if k != "seconds"} for row in full_history]
    _state_equal(net_full.model.state_dict(), net_resumed.model.state_dict())
