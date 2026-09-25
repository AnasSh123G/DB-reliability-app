"""The three bake-off candidates behind one interface.

Each candidate predicts p25 / p50 / p75 of one side's delay, in minutes, from
the float32 feature matrix in `features.FEATURES` order (the categoricals are
the first `len(CATEGORICAL)` columns, as int codes with UNKNOWN = -1).

- LightGBM: one quantile booster per level; native categoricals. LightGBM
  reads a negative categorical value as missing, which is what UNKNOWN is.
- CatBoost: one MultiQuantile model for all three levels; native categoricals
  with ordered target statistics. UNKNOWN is simply one more category.
- XGBoost: one multi-quantile model (`reg:quantileerror`); native categoricals,
  which must be non-negative, so UNKNOWN is passed as missing (NaN). Runs on
  the AMD GPU when the local build has HIP support (`device="cuda"` is also the
  HIP device name), otherwise on the CPU.

Outputs are sorted per row before anyone sees them: separately fitted
quantiles can cross, and "p25 above the median" is not a prediction anyone can
use. `fit` returns the share of validation rows that were crossed, so the
report can say how often that happened.

`reporter(step, value) -> bool` is called with the validation loss every
`REPORT_EVERY` rounds while fitting; returning True stops the fit early. The
tuner uses it for pruning and for its wall-clock deadline.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from dbrel.ml.features import CATEGORICAL, FEATURES
from dbrel.ml.metrics import QUANTILES, order_quantiles, pinball

N_CAT = len(CATEGORICAL)
EARLY_STOPPING_ROUNDS = 50
REPORT_EVERY = 25

Reporter = Callable[[int, float], bool]


class Candidate:
    library: str = ""
    device: str = "cpu"
    file_suffix: str = ""

    def __init__(self, params: dict, *, threads: int, max_rounds: int = 5000):
        self.params = dict(params)
        self.threads = threads
        self.max_rounds = max_rounds
        self.best_rounds: list[int] = []
        self.fit_seconds = 0.0

    # -- to implement -------------------------------------------------------
    def _fit(self, X, y, X_val, y_val, reporter: Reporter | None) -> None: ...
    def _predict_raw(self, X) -> np.ndarray: ...
    def save(self, prefix: Path) -> list[str]: ...
    @classmethod
    def load(cls, prefix: Path, meta: dict) -> Candidate: ...

    # -- shared -------------------------------------------------------------
    def fit(self, X, y, X_val, y_val, reporter: Reporter | None = None) -> None:
        started = time.monotonic()
        self._fit(X, y, X_val, y_val, reporter)
        self.fit_seconds = time.monotonic() - started

    def predict(self, X) -> tuple[np.ndarray, float]:
        """(n, 3) ordered p25/p50/p75, and the share of rows whose raw outputs crossed."""
        return order_quantiles(self._predict_raw(X))

    def describe(self) -> dict:
        return {
            "library": self.library,
            "device": self.device,
            "params": self.params,
            "best_rounds": self.best_rounds,
            "fit_seconds": round(self.fit_seconds, 1),
        }


# ---------------------------------------------------------------------------
# LightGBM
# ---------------------------------------------------------------------------


class LightGBMCandidate(Candidate):
    library = "lightgbm"
    file_suffix = ".txt"

    def __init__(self, params, *, threads, max_rounds=5000, quantiles=QUANTILES):
        super().__init__(params, threads=threads, max_rounds=max_rounds)
        self.quantiles = tuple(quantiles)
        self.boosters = []

    def _base_params(self) -> dict:
        return {
            "objective": "quantile",
            "metric": "quantile",
            "num_threads": self.threads,
            "verbosity": -1,
            "bagging_freq": 1,
            "deterministic": True,
            "seed": 0,
            **self.params,
        }

    def _fit(self, X, y, X_val, y_val, reporter):
        import lightgbm as lgb

        train = lgb.Dataset(X, label=y, categorical_feature=list(range(N_CAT)),
                            feature_name=list(FEATURES), free_raw_data=False)
        valid = lgb.Dataset(X_val, label=y_val, reference=train)
        self.boosters, self.best_rounds = [], []
        for alpha in self.quantiles:
            callbacks = [lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)]
            if reporter is not None:
                callbacks.append(_lgb_reporter(reporter))
            booster = lgb.train(
                {**self._base_params(), "alpha": alpha},
                train,
                num_boost_round=self.max_rounds,
                valid_sets=[valid],
                callbacks=callbacks,
            )
            self.boosters.append(booster)
            self.best_rounds.append(booster.best_iteration or booster.current_iteration())

    def _predict_raw(self, X):
        if len(self.boosters) != len(QUANTILES):
            raise RuntimeError("predicting needs all three quantile boosters")
        return np.column_stack([
            b.predict(X, num_iteration=b.best_iteration or None, num_threads=self.threads)
            for b in self.boosters
        ])

    def save(self, prefix):
        names = []
        for alpha, booster in zip(self.quantiles, self.boosters):
            path = prefix.with_name(f"{prefix.name}.q{int(alpha * 100)}.txt")
            booster.save_model(str(path), num_iteration=booster.best_iteration or None)
            names.append(path.name)
        return names

    @classmethod
    def load(cls, prefix, meta):
        import lightgbm as lgb

        self = cls(meta.get("params", {}), threads=meta.get("threads", 4))
        self.boosters = [
            lgb.Booster(model_file=str(prefix.with_name(f"{prefix.name}.q{int(a * 100)}.txt")))
            for a in QUANTILES
        ]
        return self


def _lgb_reporter(reporter: Reporter):
    import lightgbm as lgb

    def callback(env):
        step = env.iteration + 1
        if step % REPORT_EVERY:
            return
        value = env.evaluation_result_list[0][2]
        if reporter(step, value):
            raise lgb.callback.EarlyStopException(env.iteration, env.evaluation_result_list)

    callback.order = 30
    return callback


# ---------------------------------------------------------------------------
# CatBoost
# ---------------------------------------------------------------------------


def _catboost_frame(X):
    import pandas as pd

    frame = pd.DataFrame(X, columns=list(FEATURES))
    for name in CATEGORICAL:
        frame[name] = frame[name].astype(np.int32)
    return frame


class CatBoostCandidate(Candidate):
    library = "catboost"
    file_suffix = ".cbm"

    def __init__(self, params, *, threads, max_rounds=5000):
        super().__init__(params, threads=threads, max_rounds=max_rounds)
        self.model = None

    def _fit(self, X, y, X_val, y_val, reporter):
        from catboost import CatBoostRegressor, Pool

        loss = "MultiQuantile:alpha=" + ",".join(str(a) for a in QUANTILES)
        self.model = CatBoostRegressor(
            loss_function=loss,
            eval_metric=loss,
            iterations=self.max_rounds,
            od_type="Iter",
            od_wait=EARLY_STOPPING_ROUNDS,
            use_best_model=True,
            thread_count=self.threads,
            random_seed=0,
            allow_writing_files=False,
            verbose=False,
            **self.params,
        )
        train = Pool(_catboost_frame(X), label=y, cat_features=list(CATEGORICAL))
        valid = Pool(_catboost_frame(X_val), label=y_val, cat_features=list(CATEGORICAL))
        callbacks = [_CatBoostReporter(reporter)] if reporter is not None else None
        self.model.fit(train, eval_set=valid, callbacks=callbacks)
        self.best_rounds = [int(self.model.get_best_iteration() or self.model.tree_count_)]

    def _predict_raw(self, X):
        pred = self.model.predict(_catboost_frame(X), thread_count=self.threads)
        return np.asarray(pred, dtype=np.float64).reshape(len(X), len(QUANTILES))

    def save(self, prefix):
        path = prefix.with_name(prefix.name + ".cbm")
        self.model.save_model(str(path))
        return [path.name]

    @classmethod
    def load(cls, prefix, meta):
        from catboost import CatBoostRegressor

        self = cls(meta.get("params", {}), threads=meta.get("threads", 4))
        self.model = CatBoostRegressor()
        self.model.load_model(str(prefix.with_name(prefix.name + ".cbm")))
        return self


class _CatBoostReporter:
    def __init__(self, reporter: Reporter):
        self.reporter = reporter

    def after_iteration(self, info) -> bool:
        step = info.iteration
        if step % REPORT_EVERY:
            return True
        metrics = info.metrics.get("validation", {})
        if not metrics:
            return True
        value = list(metrics.values())[0][-1]
        return not self.reporter(step, value)


# ---------------------------------------------------------------------------
# XGBoost
# ---------------------------------------------------------------------------


def _xgb_matrix(X):
    X = np.array(X, dtype=np.float32, copy=True)
    cats = X[:, :N_CAT]
    cats[cats < 0] = np.nan
    return X


def xgboost_has_gpu() -> tuple[bool, str]:
    """Whether this XGBoost build can train on a GPU here, and why not if it can't."""
    try:
        import xgboost as xgb
    except ImportError as exc:  # pragma: no cover - the ml extra is installed
        return False, f"xgboost not importable: {exc}"
    info = xgb.build_info()
    if not (info.get("USE_HIP") or info.get("USE_CUDA")):
        return False, "this XGBoost build has neither HIP nor CUDA support"
    try:
        X = np.random.default_rng(0).random((256, 3)).astype(np.float32)
        d = xgb.DMatrix(X, label=X[:, 0])
        xgb.train({"device": "cuda", "tree_method": "hist", "verbosity": 0}, d, num_boost_round=2)
    except Exception as exc:  # noqa: BLE001 - any failure means "no GPU"
        return False, f"GPU training failed: {exc}".splitlines()[0]
    return True, "HIP" if info.get("USE_HIP") else "CUDA"


class XGBoostCandidate(Candidate):
    library = "xgboost"
    file_suffix = ".ubj"

    def __init__(self, params, *, threads, max_rounds=5000, device="cpu"):
        super().__init__(params, threads=threads, max_rounds=max_rounds)
        self.device = device
        self.booster = None

    def _fit(self, X, y, X_val, y_val, reporter):
        import xgboost as xgb

        types = ["c"] * N_CAT + ["q"] * (len(FEATURES) - N_CAT)
        # max_bin must agree between both matrices and the booster.
        max_bin = self.params.get("max_bin", 256)
        train = xgb.QuantileDMatrix(_xgb_matrix(X), label=y, feature_types=types,
                                    feature_names=list(FEATURES), enable_categorical=True,
                                    max_bin=max_bin)
        valid = xgb.QuantileDMatrix(_xgb_matrix(X_val), label=y_val, feature_types=types,
                                    feature_names=list(FEATURES), enable_categorical=True,
                                    max_bin=max_bin, ref=train)
        params = {
            "objective": "reg:quantileerror",
            "quantile_alpha": np.array(QUANTILES),
            "eval_metric": "quantile",
            "tree_method": "hist",
            "device": self.device,
            "nthread": self.threads,
            "seed": 0,
            "verbosity": 0,
            **self.params,
        }
        callbacks = [_XGBReporter(reporter)] if reporter is not None else []
        self.booster = xgb.train(
            params, train, num_boost_round=self.max_rounds, evals=[(valid, "validation")],
            early_stopping_rounds=EARLY_STOPPING_ROUNDS, callbacks=callbacks, verbose_eval=False,
        )
        self.best_rounds = [int(getattr(self.booster, "best_iteration", self.max_rounds - 1)) + 1]

    def _predict_raw(self, X):
        import xgboost as xgb

        types = ["c"] * N_CAT + ["q"] * (len(FEATURES) - N_CAT)
        data = xgb.DMatrix(_xgb_matrix(X), feature_types=types, feature_names=list(FEATURES),
                           enable_categorical=True)
        rounds = self.best_rounds[0] if self.best_rounds else 0
        pred = self.booster.predict(data, iteration_range=(0, rounds) if rounds else (0, 0))
        return np.asarray(pred, dtype=np.float64).reshape(len(X), len(QUANTILES))

    def save(self, prefix):
        path = prefix.with_name(prefix.name + ".ubj")
        self.booster.save_model(str(path))
        (prefix.with_name(prefix.name + ".rounds.json")).write_text(json.dumps(self.best_rounds))
        return [path.name, prefix.name + ".rounds.json"]

    @classmethod
    def load(cls, prefix, meta):
        import xgboost as xgb

        self = cls(meta.get("params", {}), threads=meta.get("threads", 4), device="cpu")
        self.booster = xgb.Booster()
        self.booster.load_model(str(prefix.with_name(prefix.name + ".ubj")))
        self.best_rounds = json.loads(prefix.with_name(prefix.name + ".rounds.json").read_text())
        return self


class _XGBReporter:
    """xgboost.callback.TrainingCallback, built lazily so importing this module stays cheap."""

    def __new__(cls, reporter: Reporter):
        import xgboost as xgb

        class Callback(xgb.callback.TrainingCallback):
            def after_iteration(self, model, epoch, evals_log):
                step = epoch + 1
                if step % REPORT_EVERY:
                    return False
                history = evals_log.get("validation", {})
                if not history:
                    return False
                value = list(history.values())[0][-1]
                return bool(reporter(step, float(value)))

        return Callback()


LIBRARIES: dict[str, type[Candidate]] = {
    "lightgbm": LightGBMCandidate,
    "catboost": CatBoostCandidate,
    "xgboost": XGBoostCandidate,
}


def mean_pinball(y: np.ndarray, pred: np.ndarray) -> float:
    return float(np.mean([pinball(y, pred[:, i], a) for i, a in enumerate(QUANTILES)]))
