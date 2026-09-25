"""The bake-off: tune, fit, pick a winner on validation, score everything on test.

Order of events, per library:
1. tune on the departure side (more samples; what search shows), within the
   library's wall-clock budget;
2. final fit on all training days, per side, with the best parameters,
   early-stopped on the validation days;
3. predict validation and test days.

The winner per side is the candidate with the lowest mean validation pinball
loss over p25/p50/p75. Test days are only ever predicted, never used to choose
anything. The averages and global baselines are scored on the same rows.

Every finished step is saved at once (`checkpoint.py`): each tuning trial, then
each side's model and predictions. A run killed at any point is continued with
`resume=True` and loses at most the step it was in. A finished side's model is
saved and dropped before the next fit, and the artifact is written by linking
the saved model files -- no model is held in memory it is not being fitted or
predicted with. Feature matrices are built per side from row indices when a
step needs them, and freed after it.

XGBoost on the GPU hardly needs the CPU, so by default it runs in a thread
alongside the CPU libraries (2 threads for it, 14 for them); the CPU libraries
always run one after another. If XGBoost has no GPU, all three run in turn with
every thread each.

The artifact is written to a temporary directory and renamed into place when
complete; only then does `CURRENT` move to it. A second run never touches the
first run's directory.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from dbrel.ml import baselines
from dbrel.ml.candidates import LIBRARIES, mean_pinball, xgboost_has_gpu
from dbrel.ml.checkpoint import LibraryCheckpoint, checkpoint_key
from dbrel.ml.dataset import Dataset
from dbrel.ml.features import FEATURES
from dbrel.ml.memory import format_memory, release_memory
from dbrel.ml.metrics import QUANTILES, score_with_breakdowns
from dbrel.ml.split import DateSplit, masks_for, restrict_to_training_vocab, split_dates
from dbrel.ml.tune import TRIAL_SAMPLE_ROWS, make_candidate, tune

SIDES = ("dep", "arr")  # departure first: it is the tuning side
CURRENT_NAME = "CURRENT"
MANIFEST_NAME = "manifest.json"
REPORT_NAME = "report.md"
GPU_THREADS = 2
TUNING_SEED = 0


class SavedWorkExists(RuntimeError):
    pass


@dataclass
class BakeoffConfig:
    libraries: tuple[str, ...] = ("lightgbm", "catboost", "xgboost")
    time_budget_s: float = 3600.0
    max_trials: int = 50
    parallel_gpu: bool = True
    keep_candidates: bool = False
    total_threads: int = os.cpu_count() or 4
    sample_rows: int = TRIAL_SAMPLE_ROWS
    max_rounds: int = 5000
    gpu: tuple[bool, str] | None = None  # override detection (tests, failed GPU check)
    gpu_check: dict | None = None  # gpu_check.run_gpu_check's result, copied into the manifest
    cpu_threads: int | None = None  # threads for CPU libraries (default: all, or all but GPU_THREADS)
    cache_dir: Path | None = None  # checkpoint root; None: a temporary one, removed afterwards
    resume: bool = False  # continue saved work; without it, existing saved work is refused
    drop_raw_features: bool = False  # free ds.features once re-coded (the caller is done with ds)


@dataclass
class _SideRows:
    y: np.ndarray  # the side's target for every dataset row, NaN where there is none
    train: np.ndarray  # row indices with a target, per set
    validation: np.ndarray
    test: np.ndarray


def _prepare(ds: Dataset) -> tuple[DateSplit, dict, dict[str, np.ndarray], dict[str, _SideRows]]:
    split = split_dates(ds.meta["rows_per_date"])
    masks = masks_for(ds.day, ds.meta["dates"], split)
    features, vocab = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    sides = {}
    for side in SIDES:
        y = ds.dep_min if side == "dep" else ds.arr_min
        has = ~np.isnan(y)
        sides[side] = _SideRows(y=y, **{name: np.flatnonzero(has & masks[name])
                                        for name in ("train", "validation", "test")})
    return split, vocab, features, sides


def _matrix(features: dict[str, np.ndarray], rows: np.ndarray) -> np.ndarray:
    """float32 (len(rows), len(FEATURES)) in FEATURES order, built only for `rows`."""
    out = np.empty((len(rows), len(FEATURES)), dtype=np.float32)
    for j, name in enumerate(FEATURES):
        out[:, j] = features[name][rows]
    return out


def _tuning_rows(train: np.ndarray, sample_rows: int) -> np.ndarray:
    """The departure-side training rows a library is tuned on (seeded, so a resume sees the same)."""
    if len(train) <= sample_rows:
        return train
    rng = np.random.default_rng(TUNING_SEED)
    return train[np.sort(rng.choice(len(train), size=sample_rows, replace=False))]


def _groups(ds: Dataset, features: dict[str, np.ndarray], rows: np.ndarray, dates: list[str]) -> dict:
    return {
        "segment": ds.segment[rows],
        "holiday": features["is_holiday"][rows].astype(int),
        "date": np.array(dates, dtype=object)[ds.day[rows]],
    }


def _run_library(library: str, ckpt: LibraryCheckpoint, features, sides, cfg, *, threads, device,
                 log) -> None:
    """Tune (or reuse the saved tuning), then fit each side not yet saved."""
    if ckpt.has_tuning():
        tuning = ckpt.load_tuning()["tuning"]
        log(f"{library}: tuning already finished ({tuning['n_trials']} trials), taking it")
    else:
        dep = sides["dep"]
        rows = _tuning_rows(dep.train, cfg.sample_rows)
        X, y = _matrix(features, rows), dep.y[rows]
        X_val, y_val = _matrix(features, dep.validation), dep.y[dep.validation]
        log(f"{library}: tuning on {device} with {threads} threads, budget "
            f"{cfg.time_budget_s / 60:.0f} min, {len(rows):,} sample rows, memory {format_memory()}")
        result = tune(
            library, X, y, X_val, y_val,
            budget_s=cfg.time_budget_s, max_trials=cfg.max_trials, threads=threads, device=device,
            sample_rows=cfg.sample_rows, max_rounds=cfg.max_rounds, seed=TUNING_SEED, log=log,
            storage=ckpt.study_url,
        )
        del X, y, X_val, y_val
        release_memory()
        tuning = result.as_dict()
        ckpt.save_tuning(tuning, device=device, threads=threads)
        log(f"{library}: tuning saved ({result.n_trials} trials + {result.n_pruned} pruned)")

    for side in SIDES:
        if ckpt.has_side(side):
            log(f"{library}: {side} side already fitted, taking it")
            continue
        rows = sides[side]
        X, y = _matrix(features, rows.train), rows.y[rows.train]
        X_val, y_val = _matrix(features, rows.validation), rows.y[rows.validation]
        log(f"{library}: final fit, {side} side, {len(y):,} rows, memory {format_memory()}")
        candidate = make_candidate(library, tuning["best_params"], threads=threads, device=device,
                                   max_rounds=cfg.max_rounds)
        candidate.fit(X, y, X_val, y_val)
        del X, y
        release_memory()
        pred_val, crossed_val = candidate.predict(X_val)
        del X_val
        X_test = _matrix(features, rows.test)
        pred_test, crossed_test = candidate.predict(X_test)
        del X_test
        pinball_val = mean_pinball(y_val, pred_val)
        ckpt.save_side(side, candidate, pred_test=pred_test, validation_mean_pinball=pinball_val,
                       crossed_share_validation=crossed_val, crossed_share_test=crossed_test,
                       threads=threads)
        log(f"{library}: {side} validation mean pinball {pinball_val:.4f} in "
            f"{candidate.fit_seconds:.0f}s, memory {format_memory()}")
        del candidate, pred_val, pred_test
        release_memory()
        log(f"{library}: {side} saved, memory {format_memory()}")
    ckpt.mark_done()


def _record(library: str, ckpt: LibraryCheckpoint) -> dict:
    """A finished library's result, read back from its checkpoint (predictions, no models)."""
    tuning = ckpt.load_tuning()
    record = {"library": library, "device": tuning["device"], "threads": tuning["threads"],
              "tuning": tuning["tuning"], "sides": {}}
    for side in SIDES:
        saved = ckpt.load_side(side)
        record["sides"][side] = {
            "fit": saved["fit"],
            "files": saved["files"],
            "validation_mean_pinball": saved["validation_mean_pinball"],
            "crossed_share_validation": saved["crossed_share_validation"],
            "crossed_share_test": saved["crossed_share_test"],
            "_pred_test": saved["_pred_test"],
        }
    return record


def _link_or_copy(source: Path, target: Path) -> None:
    """Model files are never modified after they are written, so a hard link is safe."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def run_bakeoff(ds: Dataset, models_dir: Path, cfg: BakeoffConfig, log=print) -> Path:
    started = time.monotonic()
    split, vocab, features, sides = _prepare(ds)
    if cfg.drop_raw_features:
        ds.features = None  # its categoricals were re-coded into `features`; the rest is shared
        release_memory()
    log(f"split: train {split.train[0]}..{split.train[-1]}, "
        f"validation {split.validation[0]}..{split.validation[-1]}, "
        f"test {split.test[0]}..{split.test[-1]}; memory {format_memory()}")

    models_dir.mkdir(parents=True, exist_ok=True)
    scratch = None
    if cfg.cache_dir is None:
        scratch = Path(tempfile.mkdtemp(prefix=".checkpoints-", dir=models_dir))
    root = cfg.cache_dir or scratch
    try:
        return _run(ds, models_dir, cfg, log, started, split, vocab, features, sides, root)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _run(ds, models_dir, cfg, log, started, split, vocab, features, sides, root) -> Path:
    ckpts = {
        library: LibraryCheckpoint(root, library, checkpoint_key(
            ds.meta, split.as_dict(), library, sample_rows=cfg.sample_rows, max_rounds=cfg.max_rounds))
        for library in cfg.libraries
    }
    existing = [library for library, ck in ckpts.items() if ck.exists()]
    if existing and not cfg.resume:
        raise SavedWorkExists(f"saved work exists for {', '.join(existing)} in {root} -- pass --resume "
                           "to continue it, or move those directories aside to start afresh")
    for ck in ckpts.values():
        ck.open()
    finished = [library for library in cfg.libraries if ckpts[library].is_done()]
    for library in finished:
        log(f"{library}: already finished, taking its saved result")
    to_run = [library for library in cfg.libraries if library not in finished]

    if cfg.gpu is not None:
        gpu_ok, gpu_note = cfg.gpu
    elif "xgboost" in to_run:
        gpu_ok, gpu_note = xgboost_has_gpu()
    else:
        gpu_ok, gpu_note = False, "xgboost not trained in this run"
    use_gpu = "xgboost" in to_run and gpu_ok
    log(f"xgboost GPU: {'available (' + gpu_note + ')' if gpu_ok else 'unavailable (' + gpu_note + ')'}")
    parallel = use_gpu and cfg.parallel_gpu
    cpu_libraries = [lib for lib in to_run if not (lib == "xgboost" and use_gpu)]

    errors: dict[str, str] = {}

    def run(library, threads, device):
        try:
            _run_library(library, ckpts[library], features, sides, cfg, threads=threads,
                         device=device, log=log)
            log(f"{library}: done, memory {format_memory()}")
        except Exception as exc:  # noqa: BLE001 - one library failing must not sink the others
            errors[library] = f"{type(exc).__name__}: {exc}"
            log(f"{library}: FAILED -- {errors[library]}")
        finally:
            release_memory()

    gpu_thread = None
    if use_gpu:
        if parallel:
            gpu_thread = threading.Thread(target=run, args=("xgboost", GPU_THREADS, "cuda"))
            gpu_thread.start()
        else:
            run("xgboost", cfg.total_threads, "cuda")
    cpu_threads = cfg.cpu_threads or (cfg.total_threads - GPU_THREADS if parallel else cfg.total_threads)
    for library in cpu_libraries:
        run(library, max(1, cpu_threads), "cpu")
    if gpu_thread is not None:
        gpu_thread.join()

    results = {library: _record(library, ckpts[library])
               for library in cfg.libraries if ckpts[library].is_done()}
    if not results:
        raise RuntimeError(f"every library failed: {errors}")

    # Baselines and scoring.
    scores: dict = {"candidates": {}, "baselines": {}}
    no_line = baselines.no_line_code(vocab)
    winners: dict[str, str] = {}
    for side in SIDES:
        rows = sides[side]
        y_test = rows.y[rows.test]
        groups = _groups(ds, features, rows.test, ds.meta["dates"])
        avg_pred, avg_cov = baselines.averages_baseline(features, rows.y, rows.train, rows.test, no_line)
        subsets = {"on_averages_covered": avg_cov}
        scores["baselines"].setdefault("averages", {})[side] = score_with_breakdowns(
            y_test, avg_pred, groups, covered=avg_cov)
        scores["baselines"].setdefault("global", {})[side] = score_with_breakdowns(
            y_test, baselines.global_baseline(rows.y[rows.train], len(y_test)), groups,
            subsets=subsets)
        for library, record in results.items():
            pred = record["sides"][side]["_pred_test"]
            scores["candidates"].setdefault(library, {})[side] = score_with_breakdowns(
                y_test, pred, groups, subsets=subsets)
        winners[side] = min(results, key=lambda lib: results[lib]["sides"][side]["validation_mean_pinball"])
        log(f"winner ({side}): {winners[side]}")

    # Artifact: the winners' saved model files, linked in under the names prediction loads.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = models_dir / stamp
    tmp = models_dir / f".{stamp}.partial"
    tmp.mkdir()
    model_files: dict[str, dict] = {}
    for side in SIDES:
        for library, record in results.items():
            if library != winners[side] and not cfg.keep_candidates:
                continue
            target = side if library == winners[side] else f"{side}.{library}"
            names = []
            for name in record["sides"][side]["files"]:
                if not name.startswith(side + "."):
                    raise RuntimeError(f"unexpected model file {name!r} for side {side}")
                new = target + name[len(side):]
                _link_or_copy(ckpts[library].dir / name, tmp / new)
                names.append(new)
            model_files.setdefault(side, {})[library] = names

    manifest = {
        "created_at": stamp,
        "model_version": 2,
        "features": list(FEATURES),
        "quantiles": list(QUANTILES),
        "vocab": vocab,
        "split": split.as_dict(),
        "dataset": {k: ds.meta[k] for k in ("db_path", "db_fingerprint", "built_at", "n_rows",
                                            "n_arr_targets", "n_dep_targets")},
        "winners": winners,
        "model_files": model_files,
        "gpu": {"available": gpu_ok, "note": gpu_note, "parallel": parallel, "check": cfg.gpu_check},
        "candidates": {lib: _public(rec) for lib, rec in results.items()},
        "errors": errors,
        "scores": scores,
        "versions": _library_versions(),
        "host": {"python": platform.python_version(), "cpu_count": os.cpu_count()},
        "config": {"libraries": list(cfg.libraries), "time_budget_s": cfg.time_budget_s,
                   "max_trials": cfg.max_trials, "parallel_gpu": cfg.parallel_gpu,
                   "sample_rows": cfg.sample_rows, "max_rounds": cfg.max_rounds,
                   "cpu_threads": cpu_threads, "resumed_finished": finished},
        "seconds": round(time.monotonic() - started, 1),
    }
    (tmp / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1, default=_json_default))
    from dbrel.ml.report import render_report

    (tmp / REPORT_NAME).write_text(render_report(manifest))
    tmp.rename(final)
    pointer_tmp = models_dir / (CURRENT_NAME + ".tmp")
    pointer_tmp.write_text(stamp + "\n")
    pointer_tmp.replace(models_dir / CURRENT_NAME)
    log(f"artifact: {final}")
    return final


def _public(record: dict) -> dict:
    """A result record without its raw predictions, for the manifest."""
    out = {k: v for k, v in record.items() if k != "sides"}
    out["sides"] = {
        side: {k: v for k, v in data.items() if not k.startswith("_")}
        for side, data in record["sides"].items()
    }
    return out


def _library_versions() -> dict:
    versions = {}
    for name in ("numpy", *LIBRARIES, "optuna"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", "?")
        except ImportError:
            versions[name] = None
    try:
        import xgboost

        info = xgboost.build_info()
        versions["xgboost_build"] = {k: info.get(k) for k in ("USE_HIP", "USE_CUDA")}
    except Exception:  # noqa: BLE001
        pass
    return versions


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"not JSON serialisable: {type(value)}")


def current_artifact(models_dir: Path) -> Path | None:
    pointer = models_dir / CURRENT_NAME
    if not pointer.exists():
        return None
    path = models_dir / pointer.read_text().strip()
    return path if (path / MANIFEST_NAME).exists() else None
