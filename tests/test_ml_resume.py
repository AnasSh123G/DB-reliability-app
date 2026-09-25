"""Tuning resumes from its saved study: finished trials are kept, only the rest is run."""

from __future__ import annotations

import optuna

from dbrel.ml.tune import tune

from test_ml_training import smoke_frame


def _tune(storage, max_trials, budget_s=600.0):
    X, y = smoke_frame()
    return tune("lightgbm", X[:2000], y[:2000], X[2000:], y[2000:], budget_s=budget_s,
                max_trials=max_trials, threads=2, max_rounds=40, storage=storage)


def test_a_second_call_runs_only_the_remaining_trials(tmp_path):
    url = f"sqlite:///{tmp_path / 'study.sqlite3'}"
    first = _tune(url, max_trials=2)
    assert first.n_trials + first.n_pruned == 2

    lines = []
    X, y = smoke_frame()
    second = tune("lightgbm", X[:2000], y[:2000], X[2000:], y[2000:], budget_s=600.0,
                  max_trials=4, threads=2, max_rounds=40, storage=url, log=lines.append)
    assert second.n_trials + second.n_pruned == 4
    assert [h["number"] for h in second.history] == [0, 1, 2, 3]
    assert second.history[:2] == first.history  # the first two trials were not rerun
    assert any("resuming tuning -- 2 finished trials kept" in line for line in lines)
    assert not any(line.startswith("lightgbm trial 0:") or line.startswith("lightgbm trial 1:")
                   for line in lines)
    assert second.seconds >= first.seconds


def test_an_exhausted_budget_runs_nothing_more(tmp_path):
    url = f"sqlite:///{tmp_path / 'study.sqlite3'}"
    first = _tune(url, max_trials=2)
    again = _tune(url, max_trials=10, budget_s=first.seconds * 0.5)
    assert again.n_trials + again.n_pruned == 2


def test_a_trial_that_was_running_at_the_kill_is_failed_and_ignored(tmp_path):
    url = f"sqlite:///{tmp_path / 'study.sqlite3'}"
    _tune(url, max_trials=1)
    study = optuna.load_study(study_name="lightgbm", storage=url)
    study.ask()  # left RUNNING, as a SIGKILL mid-trial leaves it
    resumed = _tune(url, max_trials=2)
    states = [t.state for t in optuna.load_study(study_name="lightgbm", storage=url).trials]
    assert states.count(optuna.trial.TrialState.FAIL) == 1
    assert optuna.trial.TrialState.RUNNING not in states
    assert resumed.n_trials + resumed.n_pruned == 2
