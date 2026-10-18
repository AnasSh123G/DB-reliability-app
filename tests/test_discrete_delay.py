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


def test_verify_training_key_rejects_modified_prepared_column(tmp_path):
    root, key, _, _, _ = _provenance_fixture(tmp_path)
    prepared = root / "data/ml/full_year/prepared" / f"{compact_provenance.PREFIX}fit" / "cats.bin"
    old = prepared.stat()
    prepared.write_bytes(np.array([4, 5, 6], dtype=np.int32).tobytes())
    os.utime(prepared, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
    assert prepared.stat().st_size == old.st_size

    with pytest.raises(ValueError, match="frozen fit prepared column changed: cats"):
        compact_provenance.verify_training_key(key, root)


