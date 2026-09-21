"""Fitting the delay model.

Three LightGBM boosters, one per quantile, fitted on the oldest 60% of service
dates; the next 20% calibrates the band; the newest 20% is never seen until the
final score. That ordering is the whole experiment — see features.split_by_date.

The run ends in a verdict, not just a model. If the newest 20% shows no real gain
over the historical average rebuilt from the fit period, the artifact is written
somewhere it can never be loaded from and the command says why. That is
deliberate: this baseline is hard to beat, and the failure mode worth guarding
against is not a model that crashes but one that quietly serves worse numbers
than the aggregate it replaced.

This module and ml/live.py are the only two allowed to import lightgbm or numpy,
and both import them lazily (inside functions, or behind a try/except at first
use) so the CLI and the web process still start without the extra.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from dbrel.config import ML_MIN_IMPROVEMENT, ML_MODEL_VERSION
from dbrel.ml import artifacts, conformal, evaluate
from dbrel.ml._common import CATEGORICAL_NAMES, FEATURE_NAMES, QUANTILES
from dbrel.ml.features import build_vocabularies, encode, extract, split_by_date

# Modest and fixed. The dataset is a few hundred thousand rows of railway
# timetable, not a Kaggle leaderboard, and an untuned forest that generalises
# beats a tuned one that has been fitted to the test period by repeated looking.
PARAMS = {
    "objective": "quantile",
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 50,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "verbose": -1,
    "num_threads": 0,
}
NUM_ROUNDS = 300


@dataclass(slots=True)
class TrainResult:
    rows: int = 0
    n_fit: int = 0
    n_calibrate: int = 0
    n_test: int = 0
    fit_dates: list[str] = field(default_factory=list)
    calibrate_dates: list[str] = field(default_factory=list)
    test_dates: list[str] = field(default_factory=list)
    conformal_adjustment: float = 0.0
    evaluation: object | None = None
    activated: bool = False
    reason: str = ""
    directory: str = ""
    importances: list[tuple[str, int]] = field(default_factory=list)
    elapsed_s: float = 0.0


def run_train(
    session: Session,
    *,
    since: str | None = None,
    until: str | None = None,
    seed: int = 0,
    dry_run: bool = False,
    now: int | None = None,
    progress=lambda msg: None,
) -> TrainResult:
    import lightgbm as lgb
    import numpy as np

    started = time.monotonic()
    result = TrainResult()

    progress("  extracting features...")
    data = extract(session, since=since, until=until, progress=progress)
    result.rows = len(data)
    if not data:
        result.reason = "no usable observations in the window"
        result.elapsed_s = time.monotonic() - started
        return result

    split = split_by_date(data.dates)
    result.n_fit, result.n_calibrate, result.n_test = (
        len(split.fit), len(split.calibrate), len(split.test)
    )
    result.fit_dates, result.calibrate_dates, result.test_dates = (
        split.fit_dates, split.calibrate_dates, split.test_dates
    )
    if not split.calibrate or not split.test:
        result.reason = "not enough distinct service dates to hold out a calibration and test period"
        result.elapsed_s = time.monotonic() - started
        return result

    vocabularies = build_vocabularies(data, split.fit)
    matrix = np.asarray(encode(data, vocabularies), dtype=np.float64)
    labels = np.asarray(data.labels, dtype=np.float64)

    fit_x, fit_y = matrix[split.fit], labels[split.fit]
    cal_x, cal_y = matrix[split.calibrate], labels[split.calibrate]
    test_x, test_y = matrix[split.test], labels[split.test]

    progress(f"  fitting {len(QUANTILES)} quantile models on {len(fit_x):,} rows...")
    boosters: dict[float, object] = {}
    for quantile in QUANTILES:
        dataset = lgb.Dataset(
            fit_x,
            label=fit_y,
            feature_name=list(FEATURE_NAMES),
            categorical_feature=list(CATEGORICAL_NAMES),
            free_raw_data=False,
        )
        boosters[quantile] = lgb.train(
            {**PARAMS, "alpha": quantile, "seed": seed, "data_random_seed": seed},
            dataset,
            num_boost_round=NUM_ROUNDS,
        )

    lo_q, mid_q, hi_q = QUANTILES

    # numpy stops here. Everything downstream — conformal, evaluate, the manifest
    # — is plain-Python by design so it stays testable without the `ml` extra,
    # and a numpy scalar leaking through reaches json.dumps as an unserialisable
    # type long after the arithmetic that produced it.
    def plain(values) -> list[float]:
        return [float(v) for v in values]

    progress("  calibrating the band...")
    widen = float(conformal.calibrate(
        plain(cal_y),
        plain(boosters[lo_q].predict(cal_x)),
        plain(boosters[hi_q].predict(cal_x)),
        alpha=1 - (hi_q - lo_q),
    ))
    result.conformal_adjustment = widen

    progress("  scoring against the historical baseline...")
    test_p50 = plain(boosters[mid_q].predict(test_x))
    band_lo, band_hi = conformal.apply(
        plain(boosters[lo_q].predict(test_x)), plain(boosters[hi_q].predict(test_x)), widen
    )
    lookup = evaluate.historical_baseline(data, split.fit)
    fallback = float(np.median(fit_y))
    baseline, cold = evaluate.baseline_predictions(data, split.test, lookup, fallback)
    result.evaluation = evaluate.evaluate(
        plain(test_y), test_p50, band_lo, band_hi, baseline, fallback, cold
    )

    gains = boosters[mid_q].feature_importance(importance_type="gain")
    result.importances = sorted(
        zip(FEATURE_NAMES, (int(g) for g in gains)), key=lambda p: -p[1]
    )

    result.activated = bool(result.evaluation.improvement >= ML_MIN_IMPROVEMENT)
    result.reason = (
        f"beat the historical average by {result.evaluation.improvement:.1%}"
        if result.activated
        else (
            f"only {result.evaluation.improvement:+.1%} against the historical average, "
            f"below the {ML_MIN_IMPROVEMENT:.0%} bar"
        )
    )

    if dry_run:
        result.reason += " (dry run, nothing written)"
        result.elapsed_s = time.monotonic() - started
        return result

    manifest = artifacts.Manifest(
        model_version=ML_MODEL_VERSION,
        model_id=artifacts.new_model_id(now),
        trained_at=now or int(time.time()),
        active=result.activated,
        feature_names=list(FEATURE_NAMES),
        quantiles=list(QUANTILES),
        conformal_adjustment=widen,
        vocabularies=vocabularies,
        fit_dates=split.fit_dates,
        calibrate_dates=split.calibrate_dates,
        test_dates=split.test_dates,
        n_fit=len(split.fit),
        n_calibrate=len(split.calibrate),
        n_test=len(split.test),
        metrics={
            "model_mae": result.evaluation.model_mae,
            "model_pinball": result.evaluation.model_pinball,
            "baseline_mae": result.evaluation.baseline_mae,
            "baseline_pinball": result.evaluation.baseline_pinball,
            "naive_mae": result.evaluation.naive_mae,
            "coverage": result.evaluation.band_coverage,
            "band_width": result.evaluation.band_width,
            "cold_start_share": result.evaluation.cold_start_share,
            "improvement": result.evaluation.improvement,
        },
        rejected_reason=None if result.activated else result.reason,
    )
    result.directory = str(artifacts.save(boosters, manifest))
    result.elapsed_s = time.monotonic() - started
    return result
