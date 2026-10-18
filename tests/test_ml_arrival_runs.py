"""Resumable arrival experiments: finished trials are kept, setups are never mixed."""

from __future__ import annotations

import numpy as np
import pytest

from dbrel.config import ML_MODELS_DIR
from dbrel.ml.arrival_runs import ArrivalExperiment, experiment_key, run_trials, trial_id
from dbrel.ml.checkpoint import CheckpointMismatch

META = {"kind": "arrival-only", "built_at": 1.0, "db_fingerprint": [1, 2], "n_rows": 4,
        "cutoff_hours": 5, "timetable_asof_verified": False}
SPLIT = {"train": ["2026-09-01"], "validation": ["2026-09-02"], "test": []}
SPACE = {"histogram": {"min_support": [5, 10]}}
TRIALS = [{"family": "histogram", "min_support": m, "point_rule": "window"} for m in (5, 10, 20)]


def _key(**changes):
    kwargs = {"search_space": SPACE, "cutoff_rule": "finalized_before_first_validation_cutoff"}
    kwargs.update(changes)
    return experiment_key(dict(META), dict(SPLIT), **kwargs)


def _runner(calls, fail_on=None):
    def run(params, scratch):
        calls.append(params["min_support"])
        if params["min_support"] == fail_on:
            raise KeyboardInterrupt  # a kill mid-trial
        (scratch / "tables.npz").write_bytes(b"x")
        return {"summary": {"within_3_min": 0.5}, "stop_id": np.array([11, 12]),
                "point": np.array([1.0, np.nan]), "missing_reason": np.array(["", "no_training_history"])}
    return run


def test_interrupted_run_resumes_only_unfinished_trials(tmp_path):
    exp = ArrivalExperiment(tmp_path, "search", _key())
    calls = []
    with pytest.raises(KeyboardInterrupt):
        run_trials(exp, TRIALS, _runner(calls, fail_on=10), max_trials=12, max_seconds=3600, log=lambda m: None)
    assert calls == [5, 10] and len(exp.completed()) == 1
    assert not list(exp.trials_dir.glob(".*"))  # nothing half-written is left behind

    calls.clear()
    done = run_trials(ArrivalExperiment(tmp_path, "search", _key()), TRIALS, _runner(calls),
                      max_trials=12, max_seconds=3600, log=lambda m: None)
    assert calls == [10, 20] and len(done) == 3
    saved = done[trial_id(TRIALS[0])]
    assert saved["params"] == TRIALS[0] and saved["files"] == ["tables.npz"]
    val = exp.load_validation(TRIALS[0])
    assert val["stop_id"].tolist() == [11, 12] and val["missing_reason"].tolist()[1] == "no_training_history"


def test_a_partial_trial_left_by_a_kill_is_discarded_and_rerun(tmp_path):
    exp = ArrivalExperiment(tmp_path, "search", _key())
    exp.open()
    partial = exp.trials_dir / f".{trial_id(TRIALS[0])}.partial"
    partial.mkdir()
    (partial / "trial.json").write_text("{}")
    calls = []
    run_trials(exp, TRIALS[:1], _runner(calls), max_trials=12, max_seconds=3600, log=lambda m: None)
    assert calls == [5] and not partial.exists()


@pytest.mark.parametrize("change,field", [
    ({"search_space": {"histogram": {"min_support": [5]}}}, "search_space"),
    ({"cutoff_rule": "service_date_only"}, "cutoff.rule"),
])
def test_different_setup_is_refused_by_name(tmp_path, change, field):
    ArrivalExperiment(tmp_path, "search", _key()).open()
    with pytest.raises(CheckpointMismatch, match=field):
        ArrivalExperiment(tmp_path, "search", _key(**change)).open()
    other_split = experiment_key(META, dict(SPLIT, validation=["2026-09-03"]), search_space=SPACE,
                                 cutoff_rule="finalized_before_first_validation_cutoff")
    with pytest.raises(CheckpointMismatch, match="split"):
        ArrivalExperiment(tmp_path, "search", other_split).open()


def test_trial_identity_and_deployed_models_are_protected():
    assert trial_id({"a": 1, "b": 2}) == trial_id({"b": 2, "a": 1})
    assert trial_id(TRIALS[0]) != trial_id(dict(TRIALS[0], point_rule="median"))
    with pytest.raises(ValueError, match="must not be written inside"):
        ArrivalExperiment(ML_MODELS_DIR, "search", _key())
    with pytest.raises(ValueError, match="arrival-only"):
        experiment_key(dict(META, kind="v2"), SPLIT, search_space=SPACE, cutoff_rule="x")
