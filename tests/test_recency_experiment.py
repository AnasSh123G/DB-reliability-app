"""Path-safety contract for selecting an isolated recency experiment directory."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _directory_fn():
    import recency_experiment
    return recency_experiment.directory


def test_default_experiment_directory_is_the_dated_repo_local_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("DBREL_RECENCY_EXPERIMENT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()

    assert _directory_fn()(repo) == repo / "data/ml/recency_training_20261013"


def test_env_selects_a_repo_relative_experiment_inside_data_ml(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    configured = "data/ml/recency_training_alternate"
    expected = repo / configured
    monkeypatch.setenv("DBREL_RECENCY_EXPERIMENT", configured)

    selected = _directory_fn()(repo)

    assert selected == expected
    assert (repo / "data/ml").resolve() in selected.resolve().parents


@pytest.mark.parametrize("configured", [
    "../outside/recency",
    "data/ml/../../outside/recency",
    "data/ml",
])
def test_env_refuses_paths_outside_the_experiment_subtree(tmp_path, monkeypatch, configured):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("DBREL_RECENCY_EXPERIMENT", configured)

    with pytest.raises(ValueError, match="data/ml|inside|experiment"):
        _directory_fn()(repo)


def test_env_refuses_data_ml_symlink_that_escapes_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    data = repo / "data"
    outside = tmp_path / "outside"
    data.mkdir(parents=True)
    outside.mkdir()
    (data / "ml").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("DBREL_RECENCY_EXPERIMENT", "data/ml/recency_training_alternate")

    with pytest.raises(ValueError, match="data/ml|inside|experiment"):
        _directory_fn()(repo)
