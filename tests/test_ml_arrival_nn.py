"""Arrival-only networks (torch; skipped outside the ROCm venv).

    PYTHONPATH=src data/build/torch-rocm/venv/bin/python -m pytest --noconftest tests/test_ml_arrival_nn.py
"""

from __future__ import annotations

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("tabm")

from dbrel.ml.arrival import arrival_inputs, fit_input_prep  # noqa: E402
from dbrel.ml.arrival_nn import ArrivalNet, NetConfig, safe_batch  # noqa: E402
from dbrel.ml.features import CATEGORICAL, NUMERIC, UNKNOWN  # noqa: E402

SMALL = dict(hidden=32, blocks=1, embedding_max_dim=4, train_emb_dim=4, tabm_k=4,
             batch_size=256, epochs=6, patience=6, lr=1e-2)


def _data(n=2000, seed=0):
    """Train t arrives 2*t minutes late (+-0.5 noise); ten trains, one station."""
    rng = np.random.default_rng(seed)
    train = rng.integers(0, 10, n).astype(np.int32)
    enc = {c: np.zeros(n, dtype=np.int32) for c in CATEGORICAL}
    enc["train"] = train
    for name in NUMERIC:
        enc[name] = rng.normal(size=n).astype(np.float32)
    enc["sched_min"] = rng.integers(0, 1440, n).astype(np.float32)
    y = (2.0 * train + rng.uniform(-0.5, 0.5, n)).astype(np.float64)
    vocab = {c: [0] for c in CATEGORICAL}
    vocab["train"] = list(range(10))
    return enc, y, vocab


@pytest.mark.parametrize("arch", ["tabm", "mlp"])
@pytest.mark.parametrize("head,rule", [("quantile", "median"), ("binned", "median"), ("binned", "window")])
def test_shapes_order_and_unknown_categories(arch, head, rule):
    enc, y, vocab = _data(64)
    enc["train"][:3] = [UNKNOWN, 99, 5]  # unknown, out of vocabulary, known
    prep = fit_input_prep(enc, np.ones(64, bool), vocab, "robust" if arch == "mlp" else "standard")
    net = ArrivalNet(NetConfig(arch=arch, head=head, point_rule=rule, **SMALL), prep)
    out = net.predict(enc)
    assert out.point.shape == (64,) and out.percentiles.shape == (64, 3)
    assert out.rule == rule and (out.missing_reason == "").all()
    assert np.isfinite(out.point).all() and np.all(np.diff(out.percentiles, axis=1) >= 0)
    cats, _ = arrival_inputs(enc, prep)
    assert cats[:3, CATEGORICAL.index("train")].tolist() == [0, 0, 6]


def test_window_rule_requires_a_distribution():
    with pytest.raises(ValueError, match="distribution"):
        NetConfig(head="quantile", point_rule="window")


def test_batch_guard_stays_under_the_rocm_fault():
    cfg = NetConfig(arch="tabm", hidden=512, tabm_k=16)
    assert safe_batch(cfg, 150, 10 ** 6) * 512 * 4 * 16 <= 2 ** 30


@pytest.mark.parametrize("arch,head,rule", [("mlp", "quantile", "median"), ("tabm", "binned", "window")])
def test_fit_learns_train_offsets_and_keeps_best_validation_epoch(arch, head, rule):
    enc, y, vocab = _data()
    rows = np.arange(len(y))
    train, val = rows[:1600], rows[1600:]
    mask = np.zeros(len(y), bool)
    mask[train] = True
    prep = fit_input_prep(enc, mask, vocab)
    cfg = NetConfig(arch=arch, head=head, point_rule=rule, unknown_rate=0.05,
                    **dict(SMALL, epochs=30, patience=5, batch_size=128))
    net = ArrivalNet(cfg, prep)
    cats, nums = arrival_inputs(enc, prep)
    history = net.fit(cats, nums, y, train, val)
    assert len(history) < cfg.epochs  # stopped early once validation stopped improving
    best = max(h["val_within_3_min"] for h in history)
    out = net.predict_arrays(cats[val], nums[val])
    within = np.mean(np.abs(out.point - y[val]) <= 3)
    assert within == pytest.approx(best)
    assert within > 0.8
    with pytest.raises(ValueError, match="recorded arrival"):
        net.fit(cats, nums, np.where(rows == 0, np.nan, y), train, val)
