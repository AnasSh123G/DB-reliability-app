"""CPU-only contracts for weight normalization and date-mixed training pools."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recency_weights as weighting  # noqa: E402


def test_fixed_dataset_mean_preserves_weight_scale_in_loss_gradients():
    import torch
    from dbrel.ml.arrival_nn import arrival_loss

    torch.set_num_threads(1)
    cfg = SimpleNamespace(head="quantile")
    raw = torch.tensor([[0.2, 0.8, 1.4], [1.0, 1.4, 2.0],
                        [2.0, 2.5, 3.0], [3.0, 3.5, 4.0]], requires_grad=True)
    target = torch.tensor([0.0, 2.0, 1.0, 5.0])
    bins = torch.zeros(4, dtype=torch.long)
    ones = torch.ones(4)

    normalized = weighting.normalized_weighted_loss
    torch.testing.assert_close(normalized(cfg, raw, target, bins, ones, 1.0),
                               arrival_loss(cfg, raw, target, bins))

    dataset_mean = 1.25
    unit_loss = normalized(cfg, raw, target, bins, ones, dataset_mean)
    high_loss = normalized(cfg, raw, target, bins, torch.full((4,), 1.5), dataset_mean)
    unit_grad = torch.autograd.grad(unit_loss, raw, retain_graph=True)[0]
    high_grad = torch.autograd.grad(high_loss, raw)[0]
    torch.testing.assert_close(high_grad, unit_grad * 1.5)

    for bad_mean in (0.0, -0.5, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="mean"):
            normalized(cfg, raw.detach(), target, bins, ones, bad_mean)


def _write_column(tmp_path, name, array):
    from dbrel.ml.full_year import FileColumn

    path = tmp_path / f"{name}.bin"
    array.tofile(path)
    width = array.shape[1] if array.ndim > 1 else 0
    return FileColumn(path, array.dtype, len(array), width)


def _mixed_data(tmp_path):
    n, rows_per_day = 40, 4
    row_ids = np.arange(n, dtype=np.int32)
    first_day = int(np.datetime64("2026-01-01", "D").astype("int64"))
    date_codes = first_day + np.repeat(np.arange(n // rows_per_day, dtype=np.int32), rows_per_day)
    arrays = {
        "cats": np.column_stack((row_ids, row_ids * 10)).astype("<i4"),
        "nums": np.column_stack((row_ids, row_ids + 0.25)).astype("<f4"),
        "y": (row_ids + 0.5).astype("<f4"),
        "day": date_codes.astype("<i4"),
        "stop_id": (900_000 + row_ids).astype("<i8"),
    }
    columns = {name: _write_column(tmp_path, name, value) for name, value in arrays.items()}

    class Data:
        cats, nums, y = columns["cats"], columns["nums"], columns["y"]

        def __len__(self):
            return len(self.y)

        def column(self, name):
            return columns[name]

    return Data(), arrays, rows_per_day


def _collect(source, seed):
    return list(source.blocks(np.random.default_rng(seed)))


def test_weighted_source_mixes_bounded_pools_reproducibly_and_preserves_alignment(
        tmp_path, monkeypatch):
    import train_recency_delay as runner
    from recency_weights import policy, weights

    torch = pytest.importorskip("torch")
    torch.set_num_threads(1)
    monkeypatch.setattr(runner, "BLOCK_ROWS", 4)
    data, arrays, rows_per_day = _mixed_data(tmp_path)
    end_date = np.datetime64("2026-01-10", "D")
    control_record = policy(str(end_date), amplitude=0.0)
    weighted_record = policy(str(end_date), amplitude=0.5)
    source_type = runner.WeightedSource
    control = source_type(data, control_record, mix_blocks=8)
    weighted = source_type(data, weighted_record, mix_blocks=8)
    repeat = source_type(data, weighted_record, mix_blocks=8)
    read_sizes = []
    original_read = weighted.read

    def bounded_read(start, stop):
        read_sizes.append(stop - start)
        return original_read(start, stop)

    weighted.read = bounded_read
    try:
        control_pools = _collect(control, 4231)
        weighted_pools = _collect(weighted, 4231)
        repeated_pools = _collect(repeat, 4231)
        assert [len(pool[2]) for pool in weighted_pools] == [32, 8]
        assert max(read_sizes) <= 4
        assert len(weighted_pools) == len(control_pools) == len(repeated_pools)

        seen = []
        for base, mixed, repeated in zip(control_pools, weighted_pools, repeated_pools):
            base_cats, base_nums, base_y, base_weight = base
            cats, nums, y, row_weight = mixed
            repeat_cats, repeat_nums, repeat_y, repeat_weight = repeated
            ids = y.astype(np.int32)
            seen.extend(ids.tolist())
            np.testing.assert_array_equal(base_cats, cats)
            np.testing.assert_array_equal(base_nums, nums)
            np.testing.assert_array_equal(base_y, y)
            np.testing.assert_array_equal(base_weight, np.ones(len(y), dtype=np.float32))
            np.testing.assert_array_equal(repeat_cats, cats)
            np.testing.assert_array_equal(repeat_nums, nums)
            np.testing.assert_array_equal(repeat_y, y)
            np.testing.assert_array_equal(repeat_weight, row_weight)

            np.testing.assert_array_equal(cats[:, 0], ids)
            np.testing.assert_array_equal(cats[:, 1], ids * 10)
            np.testing.assert_array_equal(nums[:, 0], ids)
            np.testing.assert_allclose(y, ids + 0.5)
            expected = weights(arrays["day"][ids], weighted_record)
            np.testing.assert_allclose(row_weight, expected)
            assert len(np.unique(arrays["day"][ids])) > 1

        assert sorted(seen) == list(range(40))
        assert len(set(seen)) == len(seen), "every source row appears once per epoch"
        assert len(np.unique(arrays["day"][seen])) == 10
    finally:
        control.close()
        weighted.close()
        repeat.close()


def _training_fixture(tmp_path, monkeypatch, *, mix_blocks=2):
    from test_compact_training import _tiny_net_and_data, _validation_data
    from recency_weights import policy
    import train_recency_delay as runner

    torch = pytest.importorskip("torch")
    torch.set_num_threads(1)
    monkeypatch.setattr(runner, "BLOCK_ROWS", 4)
    net, cats, nums, y = _tiny_net_and_data()
    days = np.repeat(
        np.asarray(["2026-08-20", "2026-08-21", "2026-08-22"], dtype="datetime64[D]")
        .astype("int64").astype("<i4"), 4)
    arrays = {"cats": cats, "nums": nums, "y": y, "day": days}
    columns = {name: _write_column(tmp_path, name, array) for name, array in arrays.items()}

    class Data:
        cats, nums, y = columns["cats"], columns["nums"], columns["y"]

        def __len__(self):
            return len(self.y)

        def column(self, name):
            return columns[name]

    validation = _validation_data(tmp_path / "validation", cats, nums, y)
    record = policy("2026-08-22", amplitude=0.5)
    return runner, net, Data(), validation, arrays, record, mix_blocks


def _without_elapsed(history):
    return [{key: value for key, value in row.items() if key != "seconds"} for row in history]


def _assert_tree_equal(left, right):
    import torch

    if isinstance(left, torch.Tensor):
        assert isinstance(right, torch.Tensor)
        assert torch.equal(left, right)
    elif isinstance(left, np.ndarray):
        assert isinstance(right, np.ndarray)
        np.testing.assert_array_equal(left, right)
    elif isinstance(left, dict):
        assert left.keys() == right.keys()
        for key in left:
            _assert_tree_equal(left[key], right[key])
    elif isinstance(left, (tuple, list)):
        assert type(left) is type(right) and len(left) == len(right)
        for a, b in zip(left, right):
            _assert_tree_equal(a, b)
    else:
        assert left == right


def test_runner_resume_preserves_mixed_source_training_evolution(tmp_path, monkeypatch):
    import torch

    from test_compact_training import _tiny_net_and_data
    torch.set_num_threads(1)
    runner, net, data, validation, _, record, mix_blocks = _training_fixture(tmp_path, monkeypatch)
    WeightedSource = runner.WeightedSource
    assert mix_blocks == 2
    full_source = WeightedSource(data, record, mix_blocks=mix_blocks)
    assert full_source.mean_weight == pytest.approx(
        float(weighting.weights(data.column("day")[:], record).mean()), rel=0, abs=1e-7)
    assert full_source.mix_blocks == mix_blocks
    assert full_source.block_rows == runner.BLOCK_ROWS * mix_blocks
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    key = {"toy": "constant-date blocks, fixed policy and mix_blocks=2"}
    try:
        full_history, full_best = runner.training(
            net, full_source, validation, len(data), "cpu", full_dir, key, None)
    finally:
        full_source.close()

    resumed_net, cats, nums, y = _tiny_net_and_data()
    assert len(cats) == len(nums) == len(y) == len(data)
    resume_dir = tmp_path / "resumed"
    resume_dir.mkdir()
    interrupted_source = WeightedSource(data, record, mix_blocks=mix_blocks)
    real_log = runner.log
    interrupted = False

    def interrupt_after_committed_epoch(message):
        nonlocal interrupted
        if message.startswith("epoch 1:") and not interrupted:
            interrupted = True
            raise RuntimeError("simulated interruption after epoch checkpoint")
        real_log(message)

    monkeypatch.setattr(runner, "log", interrupt_after_committed_epoch)
    try:
        with pytest.raises(RuntimeError, match="simulated interruption"):
            runner.training(resumed_net, interrupted_source, validation, len(data), "cpu",
                            resume_dir, key, None)
    finally:
        interrupted_source.close()
    first_latest = torch.load(resume_dir / "latest.pt", map_location="cpu", weights_only=False)
    assert first_latest["epoch"] == 1
    assert first_latest["key"] == key
    assert (resume_dir / first_latest["best_file"]).is_file()

    monkeypatch.setattr(runner, "log", real_log)
    resumed_source = WeightedSource(data, record, mix_blocks=mix_blocks)
    try:
        resumed_history, resumed_best = runner.training(
            resumed_net, resumed_source, validation, len(data), "cpu", resume_dir, key, None)
    finally:
        resumed_source.close()
    assert resumed_best == full_best
    assert _without_elapsed(resumed_history) == _without_elapsed(full_history)
    _assert_tree_equal(net.model.state_dict(), resumed_net.model.state_dict())
    final_full = torch.load(full_dir / "latest.pt", map_location="cpu", weights_only=False)
    final_resumed = torch.load(resume_dir / "latest.pt", map_location="cpu", weights_only=False)
    final_full.pop("history")
    final_resumed.pop("history")
    _assert_tree_equal(final_full, final_resumed)


def test_runner_keeps_constant_date_weight_scale_in_first_batch_gradient(tmp_path, monkeypatch):
    import torch

    from dataclasses import replace
    from test_compact_training import _tiny_net_and_data, _validation_data
    from recency_weights import policy
    import train_recency_delay as runner

    torch.set_num_threads(1)
    monkeypatch.setattr(runner, "BLOCK_ROWS", 4)
    _, cats, nums, y = _tiny_net_and_data()
    days = np.repeat(
        np.asarray(["2026-07-22", "2026-08-22"], dtype="datetime64[D]")
        .astype("int64").astype("<i4"), 4)
    arrays = {"cats": cats[:8], "nums": nums[:8], "y": y[:8], "day": days}
    columns = {name: _write_column(tmp_path, name, array) for name, array in arrays.items()}

    class Data:
        cats, nums, y = columns["cats"], columns["nums"], columns["y"]

        def column(self, name):
            return columns[name]

    validation = _validation_data(tmp_path / "validation", arrays["cats"], arrays["nums"], arrays["y"])
    record = policy("2026-08-22", amplitude=0.5)
    control_record = policy("2026-08-22", amplitude=0.0)
    weighted_source = runner.WeightedSource(Data(), record, mix_blocks=1)
    control_source = runner.WeightedSource(Data(), control_record, mix_blocks=1)
    assert weighted_source.mean_weight == pytest.approx(1.25, abs=1e-7)
    assert control_source.mean_weight == pytest.approx(1.0, abs=1e-7)

    # With a fixed seed, the first homogeneous date block is the same in both
    # runs. The weighted gradient must retain its row multiplier / dataset mean.
    span_order = np.random.default_rng(17).permutation(len(weighted_source.spans()))
    first_start, first_stop = weighted_source.spans()[int(span_order[0])]
    first_weight = float(weighting.weights(arrays["day"][first_start:first_stop], record)[0])
    expected_scale = first_weight / weighted_source.mean_weight
    captures = {"weighted": [], "control": []}
    active = [None]
    real_step = torch.optim.AdamW.step

    def capture_gradients(optimizer, *args, **kwargs):
        captures[active[0]].append([
            None if parameter.grad is None else parameter.grad.detach().clone()
            for group in optimizer.param_groups for parameter in group["params"]])
        return real_step(optimizer, *args, **kwargs)

    monkeypatch.setattr(torch.optim.AdamW, "step", capture_gradients)
    try:
        for label, source, amp in (("weighted", weighted_source, 0.5),
                                   ("control", control_source, 0.0)):
            active[0] = label
            net, _, _, _ = _tiny_net_and_data(seed=17)
            net.cfg = replace(net.cfg, epochs=1, patience=1, unknown_rate=0.0)
            key = {"amplitude": amp, "mix_blocks": 1}
            directory = tmp_path / label
            directory.mkdir()
            runner.training(net, source, validation, len(arrays["y"]), "cpu", directory, key, None)
    finally:
        weighted_source.close()
        control_source.close()

    assert len(captures["weighted"]) == len(captures["control"]) == 2
    for weighted_grad, control_grad in zip(captures["weighted"][0], captures["control"][0]):
        if weighted_grad is None or control_grad is None:
            assert weighted_grad is control_grad
        else:
            torch.testing.assert_close(weighted_grad, control_grad * expected_scale,
                                       rtol=2e-5, atol=2e-7)


def test_runner_rejects_mixed_pool_batch_schedule_mismatch_for_partial_span(tmp_path, monkeypatch):
    import torch

    from dataclasses import replace
    from test_compact_training import _tiny_net_and_data, _validation_data
    from recency_weights import policy
    import train_recency_delay as runner

    torch.set_num_threads(1)
    monkeypatch.setattr(runner, "BLOCK_ROWS", 4)
    base_net, cats, nums, y = _tiny_net_and_data()
    indices = np.arange(19) % len(y)
    arrays = {
        "cats": cats[indices],
        "nums": nums[indices],
        "y": y[indices],
        "day": np.full(19, np.datetime64("2026-08-22", "D").astype("int64"), dtype="<i4"),
    }
    columns = {name: _write_column(tmp_path, name, values) for name, values in arrays.items()}

    class Data:
        cats, nums, y = columns["cats"], columns["nums"], columns["y"]

        def __len__(self):
            return len(self.y)

        def column(self, name):
            return columns[name]

    assert (16, 19) == (len(Data()) - 3, len(Data()))
    source = runner.WeightedSource(Data(), policy("2026-08-22"), mix_blocks=2)
    assert source.read_block_rows == 4
    assert source.spans()[-1] == (16, 19)
    net = base_net
    net.cfg = replace(net.cfg, batch_size=3, epochs=1, patience=1, unknown_rate=0.0)
    validation = _validation_data(tmp_path / "validation", arrays["cats"], arrays["nums"], arrays["y"])
    directory = tmp_path / "run"
    directory.mkdir()
    try:
        with pytest.raises(ValueError, match="batch|mix|block|schedule"):
            runner.training(net, source, validation, len(Data()), "cpu", directory,
                            {"toy": "19 rows, block=4, mix=2, batch=3"}, None)
    finally:
        source.close()


@pytest.mark.parametrize("head", ["quantile", "binned"])
@pytest.mark.parametrize("members", [1, 3])
def test_fixed_theta_unequal_minibatch_gradients_aggregate_to_full_batch(head, members):
    import torch

    torch.set_num_threads(1)
    cfg = SimpleNamespace(head=head)
    feature_count = 3 if head == "quantile" else 5
    shape = (7, feature_count) if members == 1 else (7, members, feature_count)
    raw = torch.linspace(-0.8, 1.1, int(np.prod(shape)), dtype=torch.float32).reshape(shape)
    raw.requires_grad_()
    y = torch.tensor([0.0, 1.0, 3.0, 4.0, 2.0, 6.0, 5.0])
    y_bin = torch.tensor([0, 1, 3, 4, 2, 3, 4], dtype=torch.long)
    weight = torch.tensor([1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.25])
    dataset_mean_weight = 1.31
    loss_fn = weighting.normalized_weighted_loss

    full_loss = loss_fn(cfg, raw, y, y_bin, weight, dataset_mean_weight)
    full_grad = torch.autograd.grad(full_loss, raw, retain_graph=True)[0]
    grad_two = torch.autograd.grad(
        loss_fn(cfg, raw[:2], y[:2], y_bin[:2], weight[:2], dataset_mean_weight),
        raw, retain_graph=True)[0]
    grad_five = torch.autograd.grad(
        loss_fn(cfg, raw[2:], y[2:], y_bin[2:], weight[2:], dataset_mean_weight),
        raw)[0]
    row_count_weighted = (2 * grad_two + 5 * grad_five) / 7

    torch.testing.assert_close(row_count_weighted, full_grad, rtol=2e-6, atol=2e-7)


def test_recent_class_gradient_breaks_equal_weight_cancellation():
    import torch

    torch.set_num_threads(1)
    cfg = SimpleNamespace(head="binned")
    targets = torch.tensor([0.0, 0.0])
    bins = torch.tensor([0, 1], dtype=torch.long)
    theta_equal = torch.zeros(2, requires_grad=True)
    logits_equal = theta_equal.unsqueeze(0).expand(2, 2)
    equal_loss = weighting.normalized_weighted_loss(
        cfg, logits_equal, targets, bins, torch.ones(2), 1.0)
    equal_gradient = torch.autograd.grad(equal_loss, theta_equal)[0]
    torch.testing.assert_close(equal_gradient, torch.zeros_like(equal_gradient), atol=1e-7, rtol=0)

    theta_weighted = torch.zeros(2, requires_grad=True)
    logits_weighted = theta_weighted.unsqueeze(0).expand(2, 2)
    weighted_loss = weighting.normalized_weighted_loss(
        cfg, logits_weighted, targets, bins, torch.tensor([1.0, 1.5]), 1.25)
    weighted_gradient = torch.autograd.grad(weighted_loss, theta_weighted)[0]

    # Gradient descent lowers the old-class logit and raises the recent-class logit.
    assert weighted_gradient[0] > 0
    assert weighted_gradient[1] < 0
    assert weighted_gradient.abs().max() > 0
