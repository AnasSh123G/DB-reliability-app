"""Time-boxed Optuna search per library.

The search spaces below were reviewed and approved by Anas on 2026-09-24
(design.md D6). They are plain constants so changing a range is a one-line
edit here, not a hunt through training code.

Each trial fits on a random sample of the training days (`TRIAL_SAMPLE_ROWS`)
and is scored on the full validation days. The objective is what the library
itself optimises for all three levels: mean validation pinball loss over
p25/p50/p75 -- except LightGBM, which fits one booster per level and so is
tuned on the median booster alone (its p25/p75 boosters reuse the winning
parameters, each with its own early stopping). Trial objectives are only
compared within a library; the bake-off picks the winner from the final fits.

A trial reports its validation loss every few rounds; Optuna's median pruner
stops trials that are clearly worse than earlier ones at the same round, and
every trial stops when the library's wall-clock budget runs out. A library
that finished fewer than `UNDER_TUNED_TRIALS` trials in its budget is flagged
"under-tuned" in the report, so it is not silently compared as an equal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from dbrel.ml.candidates import LIBRARIES, Candidate, LightGBMCandidate, XGBoostCandidate
from dbrel.ml.metrics import QUANTILES, pinball

TRIAL_SAMPLE_ROWS = 3_000_000
UNDER_TUNED_TRIALS = 5
PRUNER_STARTUP_TRIALS = 3


def _lightgbm_space(trial) -> dict:
    return {
        "num_leaves": trial.suggest_int("num_leaves", 31, 1023, log=True),
        "learning_rate": trial.suggest_float("learning_rate", 0.02, 0.2, log=True),
        "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 20, 2000, log=True),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
        "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
        "lambda_l2": trial.suggest_float("lambda_l2", 0.0, 50.0),
        "cat_smooth": trial.suggest_float("cat_smooth", 1.0, 100.0),
        "max_cat_threshold": trial.suggest_int("max_cat_threshold", 16, 256),
    }


def _catboost_space(trial) -> dict:
    return {
        "depth": trial.suggest_int("depth", 4, 10),
        "learning_rate": trial.suggest_float("learning_rate", 0.02, 0.2, log=True),
        "l2_leaf_reg": trial.suggest_float("l2_leaf_reg", 1.0, 30.0, log=True),
        "rsm": trial.suggest_float("rsm", 0.5, 1.0),
        "bootstrap_type": "Bernoulli",
        "subsample": trial.suggest_float("subsample", 0.5, 1.0),
        "one_hot_max_size": trial.suggest_int("one_hot_max_size", 2, 64),
        "border_count": trial.suggest_int("border_count", 128, 254),
    }


def _xgboost_space(trial) -> dict:
    return {
        "max_depth": trial.suggest_int("max_depth", 4, 12),
        "max_leaves": trial.suggest_int("max_leaves", 0, 1023),
        "eta": trial.suggest_float("eta", 0.02, 0.2, log=True),
        "min_child_weight": trial.suggest_float("min_child_weight", 1.0, 200.0, log=True),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "subsample": trial.suggest_float("subsample", 0.5, 1.0),
        "lambda": trial.suggest_float("lambda", 0.0, 50.0),
        "max_cat_to_onehot": trial.suggest_int("max_cat_to_onehot", 1, 64),
        "max_bin": trial.suggest_int("max_bin", 128, 512),
    }


SEARCH_SPACES = {
    "lightgbm": _lightgbm_space,
    "catboost": _catboost_space,
    "xgboost": _xgboost_space,
}


@dataclass
class TuningResult:
    library: str
    best_params: dict
    best_value: float | None
    n_trials: int
    n_pruned: int
    seconds: float
    under_tuned: bool
    history: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "best_params": self.best_params,
            "best_value": self.best_value,
            "n_trials": self.n_trials,
            "n_pruned": self.n_pruned,
            "seconds": round(self.seconds, 1),
            "under_tuned": self.under_tuned,
            "history": self.history,
        }


def make_candidate(library: str, params: dict, *, threads: int, device: str = "cpu",
                   max_rounds: int = 5000, quantiles=QUANTILES) -> Candidate:
    cls = LIBRARIES[library]
    if cls is XGBoostCandidate:
        return cls(params, threads=threads, max_rounds=max_rounds, device=device)
    if cls is LightGBMCandidate:
        return cls(params, threads=threads, max_rounds=max_rounds, quantiles=quantiles)
    return cls(params, threads=threads, max_rounds=max_rounds)


def tune(
    library: str,
    X: np.ndarray,
    y: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    *,
    budget_s: float,
    max_trials: int,
    threads: int,
    device: str = "cpu",
    sample_rows: int = TRIAL_SAMPLE_ROWS,
    max_rounds: int = 5000,
    seed: int = 0,
    log=None,
    storage: str | None = None,
) -> TuningResult:
    """Search `library`'s space within `budget_s`; with `storage`, resume a saved study.

    A resumed study keeps every finished trial and spends only the budget they
    did not use. A trial that was running when the process died produced
    nothing: it is marked failed, and its partial time is not counted.
    """
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    rng = np.random.default_rng(seed)
    if len(X) > sample_rows:
        rows = np.sort(rng.choice(len(X), size=sample_rows, replace=False))
        X, y = X[rows], y[rows]

    started = time.monotonic()
    space = SEARCH_SPACES[library]
    backend = optuna.storages.RDBStorage(storage) if storage else optuna.storages.InMemoryStorage()
    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=seed),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=PRUNER_STARTUP_TRIALS),
        storage=backend,
        study_name=library,
        load_if_exists=True,
    )
    finished_states = (optuna.trial.TrialState.COMPLETE, optuna.trial.TrialState.PRUNED)
    for stale in study.get_trials(deepcopy=False, states=(optuna.trial.TrialState.RUNNING,)):
        # FrozenTrial exposes its storage id only as _trial_id.
        backend.set_trial_state_values(stale._trial_id, optuna.trial.TrialState.FAIL)
    earlier = study.get_trials(deepcopy=False, states=finished_states)
    spent_before = sum((t.datetime_complete - t.datetime_start).total_seconds() for t in earlier)
    remaining_s = budget_s - spent_before
    remaining_trials = max_trials - len(earlier)
    if earlier and log:
        log(f"{library}: resuming tuning -- {len(earlier)} finished trials kept, "
            f"{max(remaining_s, 0) / 60:.0f} min of budget left")
    deadline = started + max(remaining_s, 0)

    def objective(trial):
        params = space(trial)
        quantiles = (0.5,) if library == "lightgbm" else QUANTILES
        candidate = make_candidate(library, params, threads=threads, device=device,
                                   max_rounds=max_rounds, quantiles=quantiles)
        pruned = False

        def reporter(step: int, value: float) -> bool:
            nonlocal pruned
            trial.report(value, step)
            if time.monotonic() >= deadline:
                return True
            if trial.should_prune():
                pruned = True
                return True
            return False

        candidate.fit(X, y, X_val, y_val, reporter=reporter)
        if library == "lightgbm":
            booster = candidate.boosters[0]
            p50 = booster.predict(X_val, num_iteration=booster.best_iteration or None,
                                  num_threads=threads)
            value = pinball(y_val, p50, 0.5)
        else:
            pred = candidate._predict_raw(X_val)
            value = float(np.mean([pinball(y_val, pred[:, i], a) for i, a in enumerate(QUANTILES)]))
        trial.set_user_attr("value", value)
        trial.set_user_attr("rounds", candidate.best_rounds)
        trial.set_user_attr("seconds", round(candidate.fit_seconds, 1))
        seconds = candidate.fit_seconds
        del candidate
        from dbrel.ml.memory import format_memory, release_memory

        release_memory()
        if log:
            log(f"{library} trial {trial.number}: {value:.4f}{' (pruned)' if pruned else ''} "
                f"in {seconds:.0f}s, memory {format_memory()}")
        if pruned:
            raise optuna.TrialPruned()
        return value

    if remaining_s > 0 and remaining_trials > 0:
        study.optimize(objective, n_trials=remaining_trials, timeout=remaining_s, catch=())

    complete = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    pruned = [t for t in study.trials if t.state == optuna.trial.TrialState.PRUNED]
    if complete:
        best = study.best_trial
        best_params = {**best.params}
        if library == "catboost":
            best_params["bootstrap_type"] = "Bernoulli"
        best_value = best.value
    else:
        best_params, best_value = {}, None
    return TuningResult(
        library=library,
        best_params=best_params,
        best_value=best_value,
        n_trials=len(complete),
        n_pruned=len(pruned),
        seconds=spent_before + (time.monotonic() - started),
        under_tuned=len(complete) < UNDER_TUNED_TRIALS,
        history=[
            {"number": t.number, "value": t.user_attrs.get("value"),
             "pruned": t.state == optuna.trial.TrialState.PRUNED, "params": t.params,
             "rounds": t.user_attrs.get("rounds"), "seconds": t.user_attrs.get("seconds")}
            for t in sorted(study.get_trials(deepcopy=False, states=finished_states),
                            key=lambda t: t.number)
        ],
    )
