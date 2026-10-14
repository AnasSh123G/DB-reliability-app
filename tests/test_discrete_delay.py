"""CPU-only unit tests for the discrete delay postprocessor and export wrapper."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("tabm")
torch.set_num_threads(1)

from check_discrete_delay import DiscreteExport, discrete_quantiles  # noqa: E402
import compact_provenance  # noqa: E402


def test_discrete_quantiles_normalize_mass_and_choose_first_cdf_boundary_class():
    # CDF ties at .25 and .5 select the class that reaches the boundary.
    masses = np.array([
        [1.0, 1.0, 2.0, 0.0],
        [3.0, 3.0, 6.0, 0.0],  # same conditional distribution, unnormalized mass
    ])
    edges = np.array([-4.0, -1.0, 2.0, 7.0, 12.0])

    result = discrete_quantiles(masses, edges)

    assert result.dtype == np.float32
    assert result.shape == (2, 3)
    assert np.array_equal(result, np.array([[-4.0, -1.0, 2.0], [-4.0, -1.0, 2.0]], dtype=np.float32))
    assert np.all(np.diff(result, axis=1) >= 0)


def test_zero_probability_classes_are_skipped_and_negative_labels_are_preserved():
    probs = np.array([
        [0.0, 0.25, 0.0, 0.75, 0.0],
        [0.0, 0.50, 0.0, 0.50, 0.0],
    ])
    edges = np.array([-30.0, -12.0, -3.0, 4.0, 19.0, 40.0])

    result = discrete_quantiles(probs, edges)

    assert np.array_equal(result, np.array([[-12.0, 4.0, 4.0], [-12.0, -12.0, 4.0]], dtype=np.float32))
    assert np.all(result[1, :2] < 0)  # negative class labels survive for p25 and p50


@pytest.mark.parametrize("probs", [
    np.array([0.2, 0.8]),
    np.array([[0.2, -0.1, 0.9]]),
    np.array([[0.0, 0.0, 0.0]]),
    np.array([[0.5, np.nan, 0.5]]),
    np.array([[0.5, np.inf, 0.5]]),
])
def test_discrete_quantiles_reject_malformed_or_invalid_masses(probs):
    with pytest.raises(ValueError, match="finite nonnegative probability rows with positive mass"):
        discrete_quantiles(probs, np.arange(probs.shape[-1] + 1, dtype=np.float32))


def test_discrete_quantiles_reject_edge_count_mismatch():
    with pytest.raises(ValueError, match="bin edges differ"):
        discrete_quantiles(np.array([[0.5, 0.5]]), np.array([-1.0, 0.0]))


def test_cdf_tolerance_reaches_half_micro_below_quartile_but_not_two_micro_below():
    quartiles = (0.25, 0.5, 0.75)
    probs = np.array([[q - delta, 1.0 - (q - delta)]
                      for q in quartiles for delta in (0.5e-6, 2.0e-6)])

    result = discrete_quantiles(probs, np.array([-30.0, -29.0, -28.0]))

    for quantile_index, _ in enumerate(quartiles):
        assert result[quantile_index * 2:quantile_index * 2 + 2, quantile_index].tolist() == [-30.0, -29.0]


def test_uniform_and_seeded_palindromic_150_class_logits_choose_index_74_label_44():
    edges = np.arange(-30.0, 121.0)
    uniform = np.full((1, 150), 1 / 150, dtype=np.float64)

    rng = np.random.default_rng(4231)
    left_half = rng.normal(size=75)
    palindromic_logits = np.concatenate([left_half, left_half[::-1]])
    shifted = palindromic_logits - palindromic_logits.max()
    palindromic_probs = np.exp(shifted)[None, :]

    uniform_quantiles = discrete_quantiles(uniform, edges)
    palindromic_quantiles = discrete_quantiles(palindromic_probs, edges)

    assert uniform_quantiles[0, 1] == 44.0
    assert palindromic_quantiles[0, 1] == 44.0
    assert int(uniform_quantiles[0, 1] - edges[0]) == 74
    assert int(palindromic_quantiles[0, 1] - edges[0]) == 74


@pytest.mark.parametrize("edges", [
    np.array([-2.0, np.nan, 1.0]),
    np.array([-2.0, -2.0, 1.0]),
    np.array([-2.0, 1.0, 0.0]),
    np.array([[-2.0, 0.0, 1.0]]),
])
def test_discrete_quantiles_reject_invalid_edges(edges):
    with pytest.raises(ValueError, match="finite strictly increasing bin edges"):
        discrete_quantiles(np.array([[0.25, 0.75]]), edges)


def test_discrete_quantiles_reject_empty_class_axis_and_overflowed_positive_mass():
    with pytest.raises(ValueError, match="finite nonnegative probability rows with positive mass"):
        discrete_quantiles(np.empty((1, 0)), np.array([-1.0]))
    huge = np.finfo(np.float64).max
    with np.errstate(over="ignore"), pytest.raises(
            ValueError, match="finite nonnegative probability rows with positive mass"):
        discrete_quantiles(np.array([[huge, huge]]), np.array([-1.0, 0.0, 1.0]))


class _FixedLogits(torch.nn.Module):
    def __init__(self):
        super().__init__()
        # Uniform probabilities make the CDF's exact .25/.5/.75 ties stable.
        self.register_buffer("logits", torch.zeros(4, dtype=torch.float32))

    def forward(self, cat, num):
        assert cat.shape[0] == num.shape[0]
        return self.logits.expand(cat.shape[0], -1)


@pytest.mark.parametrize("rows", [1, 7])
def test_discrete_export_matches_numpy_for_dynamic_batch_and_returns_float32_triplets(rows):
    edges = np.array([-9.0, -2.0, 1.0, 5.0, 11.0], dtype=np.float64)
    network = SimpleNamespace(model=_FixedLogits(), edges=edges)
    exported = DiscreteExport(network).cpu().eval()
    cats = torch.zeros((rows, 3), dtype=torch.int64)
    nums = torch.zeros((rows, 2), dtype=torch.float32)

    with torch.inference_mode():
        actual = exported(cats, nums)
        masses = torch.softmax(network.model(cats, nums).float(), dim=-1).numpy()
    scientific = discrete_quantiles(masses, edges)

    assert actual.shape == (rows, 3)
    assert actual.dtype == torch.float32
    assert np.array_equal(actual.numpy(), scientific)
    assert np.array_equal(actual.numpy(), np.tile(np.array([-9.0, -2.0, 1.0], dtype=np.float32), (rows, 1)))


def _provenance_fixture(tmp_path):
    root = tmp_path / "repository"
    train_script = root / "scripts/train_compact_delay.py"
    dependency = root / "src/dbrel/ml/arrival.py"
    train_script.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    train_script.write_bytes(b"frozen train runner\n")
    dependency.write_bytes(b"frozen production dependency\n")

    trial = root / compact_provenance.TRIAL_REL
    trial.mkdir(parents=True)
    inputs = {"prep": {"numeric": ["sched_min", "stop_frac"]}, "vocab": {"train": [1, 2]}}
    input_path = trial / "inputs.json"
    input_path.write_text(json.dumps(inputs, indent=2))
    reference_path = trial / "validation.npz"
    reference_path.write_bytes(b"frozen reference predictions\n")

    key = {
        "script_sha": compact_provenance.file_hash(train_script),
        "production_code": {"src/dbrel/ml/arrival.py": compact_provenance.file_hash(dependency)},
        "input_sha": compact_provenance.file_hash(input_path),
        "reference_predictions_sha": compact_provenance.file_hash(reference_path),
    }
    prepared_root = root / "data/ml/full_year/prepared"
    for role, suffix in (("fit", "fit"), ("validation", "eval")):
        directory = prepared_root / f"{compact_provenance.PREFIX}{suffix}"
        directory.mkdir(parents=True)
        role_key = {"prep": compact_provenance.signature(inputs["prep"]),
                    "vocab": compact_provenance.signature(inputs["vocab"]), "role": role}
        metadata = {"role": role, "n": 3}
        (directory / "key.json").write_text(json.dumps(role_key))
        (directory / "meta.json").write_text(json.dumps(metadata))
        column = directory / "cats.bin"
        column.write_bytes(np.array([1, 2, 3], dtype=np.int32).tobytes())
        stat = column.stat()
        key[role] = role_key
        key[f"{role}_metadata"] = metadata
        key[f"{role}_files"] = {"cats": {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}}
    return root, key, inputs, input_path, dependency


def test_verify_training_key_accepts_matching_repository_snapshot(tmp_path):
    root, key, inputs, _, _ = _provenance_fixture(tmp_path)

    assert compact_provenance.verify_training_key(key, root) == inputs


def test_verify_training_key_rejects_same_cardinality_changed_inputs(tmp_path):
    root, key, inputs, input_path, _ = _provenance_fixture(tmp_path)
    changed = json.loads(input_path.read_text())
    original_size = input_path.stat().st_size
    changed["prep"]["numeric"][1] = "train_num"  # same number and length of feature names
    assert len(changed["prep"]["numeric"]) == len(inputs["prep"]["numeric"])
    input_path.write_text(json.dumps(changed, indent=2))
    assert input_path.stat().st_size == original_size

    with pytest.raises(ValueError, match="frozen reference changed: inputs.json"):
        compact_provenance.verify_training_key(key, root)


def test_verify_training_key_rejects_modified_prepared_column(tmp_path):
    root, key, _, _, _ = _provenance_fixture(tmp_path)
    prepared = root / "data/ml/full_year/prepared" / f"{compact_provenance.PREFIX}fit" / "cats.bin"
    old = prepared.stat()
    prepared.write_bytes(np.array([4, 5, 6], dtype=np.int32).tobytes())
    os.utime(prepared, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
    assert prepared.stat().st_size == old.st_size

    with pytest.raises(ValueError, match="frozen fit prepared column changed: cats"):
        compact_provenance.verify_training_key(key, root)


def test_verify_training_key_rejects_modified_production_dependency(tmp_path):
    root, key, _, _, dependency = _provenance_fixture(tmp_path)
    dependency.write_bytes(b"changed production dependency\n")

    with pytest.raises(ValueError, match="frozen production code changed: src/dbrel/ml/arrival.py"):
        compact_provenance.verify_training_key(key, root)
