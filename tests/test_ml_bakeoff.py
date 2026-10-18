"""The bake-off artifact and the offline prediction command, end to end on a tiny database."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import dbrel.config as config
import dbrel.db as dbmod
from dbrel.cli import app
from dbrel.ml.bakeoff import CURRENT_NAME, BakeoffConfig, run_bakeoff
from dbrel.ml.dataset import build_dataset, load_dataset
from dbrel.ml.features import all_directions
from dbrel.models import StopDelayAverage, TrainStopDelayAverage

from ml_fixture import build

runner = CliRunner()

TINY = BakeoffConfig(time_budget_s=1.0, max_trials=2, max_rounds=30, total_threads=2,
                     sample_rows=10_000, gpu=(False, "disabled in tests"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "dbrel.sqlite"
    build(db)
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "ML_DATA_DIR", tmp_path / "ml")
    monkeypatch.setattr(config, "ML_MODELS_DIR", tmp_path / "models")
    dbmod.dispose_engine()
    with dbmod.session_scope() as s:
        build_dataset(db, tmp_path / "ml", all_directions(s))
    yield tmp_path
    dbmod.dispose_engine()


def test_second_run_adds_an_artifact_and_moves_current_last(env):
    ds = load_dataset(env / "ml")
    models = env / "models"
    first = run_bakeoff(ds, models, TINY, log=lambda m: None)
    assert (models / CURRENT_NAME).read_text().strip() == first.name
    before = {p.name: p.stat().st_mtime for p in first.iterdir()}

    import time

    time.sleep(1.1)  # artifact names are second-resolution timestamps
    second = run_bakeoff(ds, models, TINY, log=lambda m: None)
    assert second != first
    assert {p.name: p.stat().st_mtime for p in first.iterdir()} == before
    assert (models / CURRENT_NAME).read_text().strip() == second.name
    assert not list(models.glob(".*.partial"))

    manifest = json.loads((second / "manifest.json").read_text())
    assert set(manifest["scores"]["candidates"]) == {"lightgbm", "catboost", "xgboost"}
    assert set(manifest["scores"]["baselines"]) == {"averages", "global"}
    assert manifest["winners"].keys() == {"dep", "arr"}
    assert manifest["gpu"]["available"] is False
    assert "Departure" in (second / "report.md").read_text()


@pytest.fixture
def trained(env):
    run_bakeoff(load_dataset(env / "ml"), env / "models", TINY, log=lambda m: None)
    return env


def test_prediction_needs_no_averages(trained):
    with dbmod.session_scope() as s:
        assert s.query(StopDelayAverage).count() == 0
        assert s.query(TrainStopDelayAverage).count() == 0
    result = runner.invoke(app, ["ml", "predict-delay", "--train", "RE 10070",
                                 "--station", "Dortmund Hbf", "--date", "2026-09-20"])
    assert result.exit_code == 0, result.output
    line = next(row for row in result.output.splitlines() if "RE 10070" in row)
    assert line.count("min (") == 2  # arrival and departure both predicted


def test_terminating_call_has_no_departure_prediction(trained):
    result = runner.invoke(app, ["ml", "predict-delay", "--train", "RE 10070",
                                 "--station", "Hamm(Westf)Hbf", "--date", "2026-09-20"])
    assert result.exit_code == 0, result.output
    line = next(row for row in result.output.splitlines() if "RE 10070" in row)
    arr_part, dep_part = line.split(" dep ")
    assert "min (" in arr_part and "min (" not in dep_part


# --- checkpoints and resume ------------------------------------------------------

# Trial-bound, not time-bound, so an interrupted and an uninterrupted run tune identically.
EXACT = BakeoffConfig(libraries=("catboost",), time_budget_s=600.0, max_trials=3, max_rounds=30,
                      total_threads=2, sample_rows=10_000, gpu=(False, "disabled in tests"))

_KILLED_RUN = """
import os, signal, sys
from dataclasses import replace
from pathlib import Path
from dbrel.ml.bakeoff import run_bakeoff
from dbrel.ml.dataset import load_dataset
import test_ml_bakeoff as t

data, models, cache, kill_on = sys.argv[1:]

def log(line):
    print(line, flush=True)
    if line.startswith(kill_on):
        os.kill(os.getpid(), signal.SIGKILL)

run_bakeoff(load_dataset(Path(data)), Path(models), replace(t.EXACT, cache_dir=Path(cache)), log=log)
"""


def _run_and_kill(env, cache, kill_on):
    import subprocess
    import sys
    from pathlib import Path

    tests_dir = Path(__file__).parent
    result = subprocess.run(
        [sys.executable, "-c", _KILLED_RUN, str(env / "ml"), str(env / "models"), str(cache), kill_on],
        capture_output=True, text=True, cwd=tests_dir,
        env={**__import__("os").environ, "PYTHONPATH": str(tests_dir)},
    )
    assert result.returncode == -9, result.stdout + result.stderr  # really SIGKILLed
    assert kill_on in result.stdout
    return result.stdout


def _candidates(artifact):
    manifest = json.loads((artifact / "manifest.json").read_text())
    return manifest["winners"], manifest["scores"]["candidates"]


def test_a_kill_after_the_departure_side_refits_only_the_arrival_side(env):
    from dataclasses import replace

    cache = env / "ml" / "runs"
    _run_and_kill(env, cache, "catboost: dep saved")
    dep_fit = cache / "catboost" / "dep.fit.json"
    mtime = dep_fit.stat().st_mtime
    assert not (cache / "catboost" / "arr.fit.json").exists()

    lines: list[str] = []
    resumed = run_bakeoff(load_dataset(env / "ml"), env / "models",
                          replace(EXACT, cache_dir=cache, resume=True), log=lines.append)
    assert any(line.startswith("catboost: tuning already finished") for line in lines)
    assert any(line.startswith("catboost: dep side already fitted") for line in lines)
    assert any(line.startswith("catboost: final fit, arr side") for line in lines)
    assert not any(line.startswith("catboost: final fit, dep side") for line in lines)
    assert not any(line.startswith("catboost trial") for line in lines)
    assert dep_fit.stat().st_mtime == mtime

    import time

    time.sleep(1.1)
    straight = run_bakeoff(load_dataset(env / "ml"), env / "models",
                           replace(EXACT, cache_dir=env / "ml" / "runs-straight"), log=lambda m: None)
    assert _candidates(resumed) == _candidates(straight)


