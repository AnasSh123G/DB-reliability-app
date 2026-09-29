"""The full-year arrival search: a compact track (deployable, <= 35 MB) and a large one (comparison only, <= 100 MB).

Needs the PyTorch-ROCm venv; `scripts/search_full_year.py` is the command line. Per track:

1. `split.rolling_split` of the full history (test week sealed, four validation weeks,
   training before), training rows cut at the earliest validation cutoff.
2. Streamed training statistics -> training vocabulary (train pruning) -> input prep ->
   prepared inputs on disk (`full_year`), one set per (scaling, numeric set, pruning).
3. The track's fixed trial list (design D7), run through the resumable `ArrivalExperiment`
   store under a 10 h cap. A network whose estimated download exceeds the track's budget is
   skipped with the reason recorded; the histogram's size is measured after fitting. After
   the fixed trials, one variant of the best network: the compact track re-runs it without
   the time-of-year inputs (the ablation), the large one with looser train pruning.
4. `ranking.json`: every trial ranked by validation MAE (full coverage first), every skip,
   every not-yet-run trial, and the reference models re-scored on the same validation rows.

`final` then refits each track's winner on training + validation for its chosen epoch
count, scores it once on the test week with the references on identical rows, and records
the test dates as inspected. `export` writes a final model as a role-tagged artifact; it
never moves `CURRENT`.
"""

from __future__ import annotations

import hashlib
import json
import resource
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

from dbrel.ml.arrival import (
    ArrivalNetPredictor, ConditionalHistogram, incumbent_forecast, prep_numeric, role_models_dir,
    save_arrival_artifact,
)
from dbrel.ml.arrival_nn import (
    ArrivalNet, BatchSource, NetConfig, estimate_asset_bytes, estimate_model_bytes, export_onnx,
)
from dbrel.ml.arrival_runs import MAE_OBJECTIVE, ArrivalExperiment, rank_trials, run_trials, trial_id
from dbrel.ml.features import ARRIVAL_NUMERIC, CATEGORICAL, NUMERIC, encode_categoricals
from dbrel.ml.full_history import CUTOFF_HOURS, FullHistory
from dbrel.ml.full_year import (
    PreparedSet, RowSet, evaluation_cutoff, evaluation_report, history_key, memory_check_path,
    prep_from_statistics, prepare, training_statistics,
)
from dbrel.ml.metrics import point_concentration, score_arrivals
from dbrel.ml.split import DateSplit, TrainPruning, record_inspected, rolling_split, training_vocabulary

NUMERIC_SETS = {"with-doy": ARRIVAL_NUMERIC, "no-doy": NUMERIC}
DEFAULT_PRUNING = TrainPruning()
LOOSE_PRUNING = TrainPruning(min_count=10)
REFERENCES = {"tabm-two-sided": "20260924T183944Z", "window-mlp": "20260926T113337Z"}
WINNER_TOLERANCE = 0.02  # prefer the smallest trial within 2% of the best validation MAE
EXPORT_SAMPLE_ROWS = 1000  # test-week inputs kept for the ONNX-vs-PyTorch check at export
DISCLOSURE = [
    "historical five-hour-cutoff replay, not a logged prospective forecast",
    "timetable as of each cutoff not verified (present-day patterns)",
    "time of year is validated only on the weeks adjacent to training; no calendar date has "
    "been seen in two years yet",
]


def net(arch: str, head: str, hidden: int, blocks: int, train_emb: int, *, k: int = 8,
        emb_max: int = 32, numeric: str = "with-doy", pruning: TrainPruning = DEFAULT_PRUNING) -> dict:
    cfg = NetConfig(arch=arch, head=head, point_rule="median", hidden=hidden, blocks=blocks,
                    train_emb_dim=train_emb, tabm_k=k, embedding_max_dim=emb_max)
    return {"family": "net", "config": cfg.as_dict(), "scaling": "robust" if arch == "mlp" else "standard",
            "numeric": numeric, "pruning": pruning.as_dict()}


def histogram(min_support: int = 20) -> dict:
    return {"family": "histogram", "min_support": min_support, "point_rule": "median",
            "scaling": "standard", "numeric": "with-doy", "pruning": DEFAULT_PRUNING.as_dict()}


@dataclass(frozen=True)
class Track:
    name: str
    role: str
    budget: int
    trials: tuple[dict, ...]
    variant: str  # "ablation": best network without time of year; "loose-pruning": min_count 10
    max_hours: float = 10.0

    def variant_of(self, params: dict) -> dict:
        if self.variant == "ablation":
            return {**params, "numeric": "no-doy"}
        return {**params, "pruning": LOOSE_PRUNING.as_dict()}


TRACKS = {
    "compact": Track("compact", "deployable", 35_000_000, (
        net("mlp", "quantile", 512, 3, 16),
        net("mlp", "quantile", 512, 3, 32),
        net("mlp", "quantile", 1024, 3, 32),
        net("mlp", "binned", 512, 3, 32),
        net("tabm", "quantile", 256, 2, 32, k=8),
        net("tabm", "quantile", 512, 2, 32, k=16),
        histogram(20),
    ), variant="ablation"),
    "large": Track("large", "comparison", 100_000_000, (
        net("mlp", "quantile", 1024, 4, 64, emb_max=64),
        net("mlp", "quantile", 2048, 3, 64, emb_max=64),
        net("tabm", "quantile", 1024, 3, 64, k=16, emb_max=64),
    ), variant="loose-pruning"),
}


def log_line(message: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%SZ}] {message}", flush=True)


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def peak_rss() -> int:
    """This process's resident-set high-water mark in bytes (Linux reports KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


class SearchData:
    """Statistics, vocabularies, preps and prepared inputs of one split, built on first use."""

    def __init__(self, history: FullHistory, train: RowSet, evaluate: RowSet, work: Path, *,
                 tag: str, replace_inputs: bool = False, log=log_line):
        self.history, self.train, self.evaluate = history, train, evaluate
        self.work, self.tag, self.replace_inputs, self.log = Path(work), tag, replace_inputs, log
        self._stats = None

    @property
    def stats(self) -> dict:
        if self._stats is None:
            self.log(f"training statistics over {len(self.train.dates)} dates ...")
            self._stats = training_statistics(self.history, self.train)
            self.log(f"  {self._stats['rows']:,} training rows")
        return self._stats

    def vocab(self, params: dict) -> dict[str, list]:
        return training_vocabulary(self.stats["categories"], self.stats["last_day"],
                                   TrainPruning(**params["pruning"]))

    def prep(self, params: dict) -> dict:
        prep = prep_from_statistics(self.stats, self.vocab(params), params["scaling"],
                                    NUMERIC_SETS[params["numeric"]])
        prep["train_pruning"] = params["pruning"]
        return prep

    def prepared(self, params: dict) -> tuple[PreparedSet, PreparedSet]:
        p = params["pruning"]
        name = f"{self.tag}-{params['scaling']}-{params['numeric']}-mc{p['min_count']}-rd{p['recent_days']}"
        vocab, prep = self.vocab(params), self.prep(params)
        root = self.work / "prepared"
        sets = []
        for role, rows in (("fit", self.train), ("eval", self.evaluate)):
            self.log(f"prepared inputs {name}-{role} ...")
            sets.append(prepare(self.history, rows, vocab, prep, root, f"{name}-{role}",
                                replace=self.replace_inputs))
        return sets[0], sets[1]


def estimate_bytes(data: SearchData, params: dict) -> int | None:
    """Pre-trial download estimate of a network; None for the histogram (measured after fitting)."""
    if params["family"] != "net":
        return None
    return estimate_asset_bytes(NetConfig(**params["config"]), data.prep(params), data.vocab(params))


def _stop_ids(prepared: PreparedSet) -> np.ndarray:
    return np.asarray(prepared.column("stop_id"))


def _validation_summary(y: np.ndarray, point: np.ndarray) -> dict:
    return {**score_arrivals(np.asarray(y, dtype=np.float64), point), **point_concentration(point)}


def _fit_histogram(params: dict, fit_set: PreparedSet) -> ConditionalHistogram:
    model = ConditionalHistogram(min_support=params["min_support"], point_rule=params["point_rule"])
    encoded = fit_set.histogram_encoded()
    return model.fit(encoded, np.asarray(fit_set.y), np.ones(len(fit_set), dtype=bool))


def _sources(fit_set: PreparedSet, eval_set: PreparedSet | None):
    fit = BatchSource(fit_set.cats, fit_set.nums, fit_set.y)
    return fit, (BatchSource(eval_set.cats, eval_set.nums, eval_set.y) if eval_set is not None else None)


def run_trial(data: SearchData, params: dict, scratch: Path, *, device: str, log=log_line) -> dict:
    """Fit one trial on the training rows and predict the validation rows (the `run_trials` contract)."""
    fit_set, eval_set = data.prepared(params)
    vocab, prep = data.vocab(params), data.prep(params)
    y_val = np.asarray(eval_set.y)
    if params["family"] == "histogram":
        model = _fit_histogram(params, fit_set)
        forecast = model.predict(eval_set.histogram_encoded())
        files = model.export(scratch)
        asset = sum((scratch / f).stat().st_size for f in files) + len(json.dumps(vocab, indent=1))
        extra = {"asset_basis": "measured", "levels": [lv.name for lv in model.levels]}
    else:
        cfg = NetConfig(**params["config"])
        network = ArrivalNet(cfg, prep)
        fit, val = _sources(fit_set, eval_set)
        network.fit_source(fit, val, device, log=lambda e: log(f"  epoch {json.dumps(e)}"))
        forecast = network.predict_source(val, device)
        torch.save(network.model.state_dict(), scratch / "model.pt")
        asset = estimate_asset_bytes(cfg, prep, vocab)
        extra = {"asset_basis": "estimate", "best_epoch": network.best_epoch(), "history": network.history,
                 "n_parameters": network.n_parameters()}
    (scratch / "inputs.json").write_text(json.dumps({"prep": prep, "vocab": vocab}))
    summary = {"validation": _validation_summary(y_val, forecast.point), "asset_bytes": int(asset),
               "fit_rows": len(fit_set), "vocab_trains": len(vocab["train"]), **extra}
    return {"summary": summary, "stop_id": _stop_ids(eval_set), "point": forecast.point,
            "missing_reason": forecast.missing_reason}


# ---------------------------------------------------------------------------
# references: the deployed-era artifacts, re-scored on the same rows
# ---------------------------------------------------------------------------

def _reencode(ds, vocab: dict[str, list]) -> dict[str, np.ndarray]:
    raw = {}
    for name in CATEGORICAL:
        values = ds.meta["vocab"][name]
        table = np.array(values, dtype=object if any(isinstance(v, str) for v in values) else np.int64)
        raw[name] = table[ds.features[name]] if len(table) else np.zeros(len(ds.arr_min), dtype=np.int64)
    encoded = encode_categoricals(raw, vocab)
    for name in ARRIVAL_NUMERIC:
        encoded[name] = ds.features[name]
    return encoded


def reference_points(history: FullHistory, dates: tuple[str, ...], stop_ids: np.ndarray,
                     artifacts: dict[str, Path], *, dates_per_load: int = 7) -> dict[str, np.ndarray]:
    """Each reference's point estimate for the rows `stop_ids`, in that order.

    Loaded a week at a time so the full validation range is never one in-memory dataset.
    """
    order = np.argsort(stop_ids)
    out = {name: np.full(len(stop_ids), np.nan) for name in artifacts}
    for s in range(0, len(dates), dates_per_load):
        ds = history.load(dates[s:s + dates_per_load])
        pos = order[np.searchsorted(stop_ids[order], ds.stop_id)]
        if not np.array_equal(stop_ids[pos], ds.stop_id):
            raise ValueError("reference rows differ from the evaluated rows")
        for name, artifact in artifacts.items():
            manifest = json.loads((artifact / "manifest.json").read_text())
            if manifest.get("kind") == "arrival-net":
                forecast = ArrivalNetPredictor(artifact, manifest).predict(_reencode(ds, manifest["vocab"]))
            else:
                forecast = incumbent_forecast(ds, np.arange(len(ds.arr_min)), artifact)
            out[name][pos] = forecast.point
    return out


def reference_artifacts(models_dir: Path) -> tuple[dict[str, Path], dict[str, str]]:
    found, missing = {}, {}
    for name, stamp in REFERENCES.items():
        path = Path(models_dir) / stamp
        if (path / "manifest.json").exists():
            found[name] = path
        else:
            missing[name] = f"{path} not found"
    return found, missing


# ---------------------------------------------------------------------------
# the search
# ---------------------------------------------------------------------------

def experiment_name(track: Track, split: DateSplit) -> str:
    """New data (a new test week) starts a new experiment instead of clashing with the saved key."""
    return f"{track.name}-{split.test[-1]}"


def experiment_key(history: FullHistory, split: DateSplit, cutoff: int, track: Track) -> dict:
    return {
        "dataset": {"kind": "arrival-full", **history_key(history)},
        "split": split.as_dict(),
        "cutoff": {"hours": CUTOFF_HOURS, "finalized_before": cutoff, "timetable_asof_verified": False},
        "features": list(CATEGORICAL + ARRIVAL_NUMERIC),
        "search_space": {"track": track.name, "role": track.role, "budget": track.budget,
                         "trials": list(track.trials), "variant": track.variant},
        "objective": MAE_OBJECTIVE,
    }


@dataclass
class SearchSetup:
    history: FullHistory
    split: DateSplit
    cutoff: int
    work: Path


def setup(history_root: Path, work: Path, inspected: list[str], *, test_days: int = 7, val_days: int = 28,
          min_train_days: int = 90) -> SearchSetup:
    history = FullHistory(history_root)
    split = rolling_split(history.rows_per_date, inspected, test_days=test_days, val_days=val_days,
                          min_train_days=min_train_days)
    return SearchSetup(history, split, evaluation_cutoff(history, split.validation), Path(work))


def _network_trials(completed: dict[str, dict]) -> dict[str, dict]:
    return {tid: t for tid, t in completed.items() if t["params"]["family"] == "net"}


def search_track(s: SearchSetup, track: Track, *, device: str, models_dir: Path, max_trials: int | None = None,
                 max_hours: float | None = None, replace_inputs: bool = False, log=log_line) -> dict:
    """Run (or resume) one track and write its `ranking.json`; returns the ranking."""
    data = SearchData(s.history, RowSet(s.split.train, s.cutoff), RowSet(s.split.validation), s.work,
                      tag=f"search-{s.split.test[-1]}", replace_inputs=replace_inputs, log=log)
    exp = ArrivalExperiment(s.work / "runs", experiment_name(track, s.split),
                            experiment_key(s.history, s.split, s.cutoff, track))
    exp.open()
    max_trials = max_trials if max_trials is not None else len(track.trials) + 1
    max_seconds = 3600 * (max_hours if max_hours is not None else track.max_hours)
    skipped = {}

    def within_budget(params: dict) -> bool:
        estimate = estimate_bytes(data, params)
        if estimate is not None and estimate > track.budget:
            skipped[trial_id(params)] = {"params": params, "estimate_bytes": estimate,
                                         "reason": f"estimated {estimate:,} bytes > {track.budget:,}-byte budget"}
            log(f"skip {trial_id(params)}: {skipped[trial_id(params)]['reason']}")
            return False
        return True

    def run(params: dict, scratch: Path) -> dict:
        log(f"trial {trial_id(params)}: {json.dumps(params)}")
        return run_trial(data, params, scratch, device=device, log=log)

    runnable = [p for p in track.trials if within_budget(p)]
    run_trials(exp, runnable, run, max_trials=max_trials, max_seconds=max_seconds, log=log)
    nets = _network_trials(exp.completed())
    planned = list(track.trials)
    if nets and all(exp.has(p) or trial_id(p) in skipped for p in track.trials):
        variant = track.variant_of(nets[rank_trials(nets)[0]]["params"])
        planned.append(variant)
        if within_budget(variant):
            run_trials(exp, [variant], run, max_trials=max_trials, max_seconds=max_seconds, log=log)

    references, missing = reference_artifacts(models_dir)
    ref_file = exp.dir / "references.json"
    if ref_file.exists():
        ref_scores = json.loads(ref_file.read_text())
    else:
        _, eval_set = data.prepared(planned[0])
        eval_ids = _stop_ids(eval_set)
        log(f"scoring references {sorted(references)} on {len(eval_ids):,} validation rows ...")
        points = reference_points(s.history, s.split.validation, eval_ids, references)
        y = np.asarray(eval_set.y)
        ref_scores = {name: {"artifact": str(references[name]), "validation": _validation_summary(y, p)}
                      for name, p in points.items()}
        ref_scores.update({name: {"missing": why} for name, why in missing.items()})
        ref_file.write_text(json.dumps(ref_scores, indent=1))
    return write_ranking(exp, track, s, planned, skipped, ref_scores)


def write_ranking(exp: ArrivalExperiment, track: Track, s: SearchSetup, planned: list[dict], skipped: dict,
                  references: dict) -> dict:
    completed = exp.completed()
    order = rank_trials(completed)
    ranking = {
        "track": track.name, "role": track.role, "budget_bytes": track.budget, "experiment": str(exp.dir),
        "split": {k: [v[0], v[-1], len(v)] for k, v in s.split.as_dict().items()},
        "finalized_before": s.cutoff, "objective": MAE_OBJECTIVE,
        "trials": [{"rank": i + 1, "id": tid, "params": completed[tid]["params"],
                    "validation": completed[tid]["summary"]["validation"],
                    "asset_bytes": completed[tid]["summary"]["asset_bytes"],
                    "asset_basis": completed[tid]["summary"].get("asset_basis"),
                    "best_epoch": completed[tid]["summary"].get("best_epoch"),
                    "seconds": completed[tid]["seconds"]} for i, tid in enumerate(order)],
        "skipped": list(skipped.values()),
        "not_run": [p for p in planned if trial_id(p) not in completed and trial_id(p) not in skipped],
        "references": references,
        "chosen": choose_winner(completed) if completed else None,
    }
    (exp.dir / "ranking.json").write_text(json.dumps(ranking, indent=1))
    return ranking


def choose_winner(completed: dict[str, dict], tolerance: float = WINNER_TOLERANCE) -> str:
    """The smallest full-coverage trial within `tolerance` of the best validation MAE."""
    order = rank_trials(completed)
    best = completed[order[0]]["summary"]
    if best["validation"]["coverage"] < 1.0 or best["validation"]["mae_scored"] is None:
        return order[0]
    limit = best["validation"]["mae_scored"] * (1 + tolerance)
    close = [tid for tid in order if completed[tid]["summary"]["validation"]["coverage"] >= 1.0
             and completed[tid]["summary"]["validation"]["mae_scored"] <= limit]
    return min(close, key=lambda tid: (completed[tid]["summary"]["asset_bytes"], order.index(tid)))


# ---------------------------------------------------------------------------
# final refit and the sealed test week
# ---------------------------------------------------------------------------

def final(s: SearchSetup, tracks: list[Track], *, device: str, models_dir: Path, inspected_path: Path,
          replace_inputs: bool = False, log=log_line) -> dict:
    """Refit each track's winner on training + validation, score all on the test week once."""
    refit_rows = RowSet(s.split.train + s.split.validation, evaluation_cutoff(s.history, s.split.test))
    data = SearchData(s.history, refit_rows, RowSet(s.split.test), s.work, tag=f"final-{s.split.test[-1]}",
                      replace_inputs=replace_inputs, log=log)
    out_dir = s.work / "final" / s.split.test[-1]
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} exists: this test week was already scored")
    staging = out_dir.with_name(f".{out_dir.name}.partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    predictions, winners, test_set = {}, {}, None
    for track in tracks:
        exp = ArrivalExperiment(s.work / "runs", experiment_name(track, s.split),
                                experiment_key(s.history, s.split, s.cutoff, track))
        if not (exp.dir / "key.json").exists():
            raise FileNotFoundError(f"no {track.name} search for this split in {exp.dir}")
        exp.open()
        completed = exp.completed()
        if not completed:
            raise ValueError(f"the {track.name} search has no finished trial")
        tid = choose_winner(completed)
        params, summary = completed[tid]["params"], completed[tid]["summary"]
        log(f"{track.name}: refitting trial {tid} on {len(refit_rows.dates)} dates ...")
        fit_set, test_set = data.prepared(params)
        model_dir = staging / track.name
        model_dir.mkdir()
        vocab, prep = data.vocab(params), data.prep(params)
        if params["family"] == "histogram":
            model = _fit_histogram(params, fit_set)
            point = model.predict(test_set.histogram_encoded()).point
            files = model.export(model_dir)
            asset = {"bytes": sum((model_dir / f).stat().st_size for f in files) + len(json.dumps(vocab, indent=1)),
                     "basis": "measured"}
        else:
            cfg = NetConfig(**params["config"])
            network = ArrivalNet(cfg, prep)
            fit, _ = _sources(fit_set, None)
            network.fit_source(fit, None, device, epochs=summary["best_epoch"],
                               log=lambda e: log(f"  epoch {json.dumps(e)}"))
            point = network.predict_source(_sources(test_set, None)[0], device).point
            torch.save(network.model.state_dict(), model_dir / "model.pt")
            asset = {"bytes": estimate_asset_bytes(cfg, prep, vocab), "basis": "estimate"}
        (model_dir / "inputs.json").write_text(json.dumps({"prep": prep, "vocab": vocab, "params": params}))
        np.save(model_dir / "sample_cats.npy", test_set.cats[:EXPORT_SAMPLE_ROWS])
        np.save(model_dir / "sample_nums.npy", test_set.nums[:EXPORT_SAMPLE_ROWS])
        predictions[f"{track.name}-winner"] = point
        winners[track.name] = {"trial": tid, "params": params, "validation": summary["validation"],
                               "best_epoch": summary.get("best_epoch"), "asset": asset, "role": track.role}
    stop_ids = _stop_ids(test_set)
    references, missing = reference_artifacts(models_dir)
    predictions.update(reference_points(s.history, s.split.test, stop_ids, references))
    report = evaluation_report(s.history, test_set, predictions)
    result = {
        "test_dates": list(s.split.test), "rows": len(stop_ids), "rows_sha": _sha(stop_ids.tolist()),
        "winners": winners, "scores": report,
        "reference_assets": {name: _asset_bytes(p) for name, p in references.items()},
        "missing_references": missing, "disclosure": DISCLOSURE,
    }
    np.save(staging / "stop_id.npy", stop_ids)
    (staging / "final.json").write_text(json.dumps(result, indent=1))
    staging.rename(out_dir)
    record_inspected(inspected_path, s.split.test, f"final test of {', '.join(t.name for t in tracks)} "
                                                   f"({out_dir})")
    return result


def _asset_bytes(artifact: Path) -> int | None:
    manifest = json.loads((artifact / "manifest.json").read_text())
    return manifest.get("asset_bytes", manifest.get("size_bytes"))


def _save_array(path: Path, array: np.ndarray, dtype: str) -> dict:
    data = np.ascontiguousarray(array, dtype=dtype)
    path.write_bytes(data.tobytes())
    return {"file": path.name, "dtype": dtype, "shape": list(data.shape)}


def write_browser_check(out: Path, name: str, check: dict, onnx_dir: Path, cats: np.ndarray,
                        nums: np.ndarray) -> None:
    """The `scripts/browser_check.py` layout for one exported network, on its test-week reference rows."""
    shutil.rmtree(out, ignore_errors=True)
    (out / name).mkdir(parents=True)
    for f in ("model.onnx", "prep.json"):
        shutil.copyfile(onnx_dir / f, out / name / f)
    ref = out / "reference"
    ref.mkdir()
    scaling = check["scaling"]
    reference = {
        "rows": len(cats),
        "inputs": {f"cat_{scaling}": _save_array(ref / f"cat_{scaling}.i64", cats, "<i8"),
                   f"num_{scaling}": _save_array(ref / f"num_{scaling}.f32", nums, "<f4")},
        "expected": {name: {k: _save_array(ref / f"{name}.{k}.f32", v, "<f4")
                            for k, v in check["_expected"].items()}},
    }
    (ref / "reference.json").write_text(json.dumps(reference))
    entry = {k: v for k, v in check.items() if k != "_expected"}
    (out / "manifest.json").write_text(json.dumps({"candidates": {name: entry}}, indent=1))


def export_final(final_dir: Path, track: Track, *, models_dir: Path, served_dir: Path | None = None,
                 log=log_line) -> Path:
    """Write a track's refitted winner as a role-tagged artifact (never moves `CURRENT`)."""
    result = json.loads((final_dir / "final.json").read_text())
    winner = result["winners"][track.name]
    model_dir = final_dir / track.name
    inputs = json.loads((model_dir / "inputs.json").read_text())
    if winner["params"]["family"] != "net":
        raise ValueError("only network winners export to the browser's ONNX artifact")
    cfg = NetConfig(**winner["params"]["config"])
    network = ArrivalNet(cfg, inputs["prep"])
    network.model.load_state_dict(torch.load(model_dir / "model.pt", map_location="cpu"))
    cats, nums = np.load(model_dir / "sample_cats.npy"), np.load(model_dir / "sample_nums.npy")
    onnx_dir = model_dir / "onnx"
    shutil.rmtree(onnx_dir, ignore_errors=True)
    check = export_onnx(network, cats, nums, onnx_dir)
    if not check["onnx_passed"]:
        raise ValueError(f"ONNX output differs from PyTorch by {check['onnx_vs_torch_max_abs']}")
    write_browser_check(model_dir / "browser_check", track.name, check, onnx_dir, cats, nums)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"final": str(final_dir), "trial": winner["trial"]}, "config": cfg.as_dict(),
        "prep": inputs["prep"], "vocab": inputs["vocab"], "output": check["output"],
        "edges": network.edges.tolist() if check["output"] == "probs" else None,
        "point_rule": cfg.point_rule, "features": list(CATEGORICAL) + list(prep_numeric(inputs["prep"])),
        "scores": {"validation": winner["validation"],
                   "test": result["scores"][f"{track.name}-winner"]["overall"]},
        "disclosure": result["disclosure"], "onnx_check": {k: v for k, v in check.items() if k != "_expected"},
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = role_models_dir(track.role, Path(models_dir))
    target.mkdir(parents=True, exist_ok=True)
    artifact = save_arrival_artifact(target, manifest, {"model.onnx": onnx_dir / "model.onnx"}, stamp,
                                     role=track.role, served_dir=served_dir or models_dir)
    log(f"{track.name}: {track.role} artifact {artifact}")
    return artifact


# ---------------------------------------------------------------------------
# memory probe
# ---------------------------------------------------------------------------

def probe_dates(dates: tuple[str, ...], rows_per_date: dict[str, int], rows: int) -> tuple[str, ...]:
    """The earliest dates holding at least `rows` rows."""
    total, out = 0, []
    for d in dates:
        out.append(d)
        total += rows_per_date[d]
        if total >= rows:
            break
    return tuple(out)


def widest_trial(track: Track, data: SearchData) -> dict:
    nets = [p for p in list(track.trials) if p["family"] == "net"]
    return max(nets, key=lambda p: estimate_model_bytes(NetConfig(**p["config"]), data.prep(p)))


def probe(s: SearchSetup, track: Track, train_rows: int, *, device: str, models_dir: Path, work: Path) -> dict:
    """One probe: the track's steps at `train_rows` training rows (validation scaled alike), one epoch."""
    per_date = s.history.rows_per_date
    full_train = sum(per_date[d] for d in s.split.train)
    full_val = sum(per_date[d] for d in s.split.validation)
    train_dates = probe_dates(s.split.train, per_date, train_rows)
    val_dates = probe_dates(s.split.validation, per_date, int(train_rows * full_val / max(1, full_train)))
    data = SearchData(s.history, RowSet(train_dates, s.cutoff), RowSet(val_dates), work, tag="probe",
                      replace_inputs=True, log=lambda m: None)
    started = time.monotonic()
    params = widest_trial(track, data)
    fit_set, eval_set = data.prepared(params)
    if any(p["family"] == "histogram" for p in track.trials):
        hist = next(p for p in track.trials if p["family"] == "histogram")
        _fit_histogram(hist, data.prepared(hist)[0]).predict(data.prepared(hist)[1].histogram_encoded())
    network = ArrivalNet(replace(NetConfig(**params["config"]), epochs=1), data.prep(params))
    network.fit_source(*_sources(fit_set, eval_set), device)
    references, _ = reference_artifacts(models_dir)
    reference_points(s.history, val_dates, _stop_ids(eval_set), references)
    return {"train_rows": len(fit_set), "validation_rows": len(eval_set), "peak": peak_rss(),
            "seconds": round(time.monotonic() - started, 1), "trial": trial_id(params)}


def project_peak(probes: list[dict], full_rows: int) -> int:
    """Peak at `full_rows` training rows, linear through the two largest probes."""
    (n1, p1), (n2, p2) = sorted((p["train_rows"], p["peak"]) for p in probes)[-2:]
    slope = max(0.0, (p2 - p1) / (n2 - n1)) if n2 > n1 else 0.0
    return int(p2 + slope * (full_rows - n2))


PROBE_ROWS = (1_000_000, 2_000_000)


def memory_check(history_root: Path, work: Path, track: Track, *, inspected: list[str], split_args: dict,
                 device: str, models_dir: Path, sizes: tuple[int, ...] = PROBE_ROWS, log=log_line) -> dict:
    """Probe the track at `sizes` training rows, each in its own process, and project its peaks.

    Two projections: the search (training rows) and the final refit (training + validation
    rows). Written to `memory_check-<track>.json` in `work`, where the launch gate reads it.
    """
    s = setup(history_root, work, inspected, **split_args)
    probes = []
    for rows in sizes:
        spec = {"history": str(history_root), "work": str(work), "inspected": inspected, "split": split_args,
                "track": {**track.__dict__, "trials": list(track.trials)}, "rows": rows, "device": device,
                "models_dir": str(models_dir)}
        log(f"{track.name}: memory probe at {rows:,} training rows ...")
        proc = subprocess.run([sys.executable, "-m", "dbrel.ml.full_year_search", json.dumps(spec)],
                              capture_output=True, text=True)
        if proc.returncode == -9:
            probes.append({"killed": True, "train_rows": rows, "peak": None})
            log("  killed at the memory cap")
            continue
        if proc.returncode != 0:
            raise RuntimeError(f"memory probe at {rows:,} rows failed:\n{proc.stderr[-3000:]}")
        probes.append({"killed": False, **json.loads(proc.stdout.strip().splitlines()[-1])})
        log(f"  peak {probes[-1]['peak'] / 2**30:.2f} GiB at {probes[-1]['train_rows']:,} rows")
    per_date = s.history.rows_per_date
    train_rows = sum(per_date[d] for d in s.split.train)
    refit_rows = train_rows + sum(per_date[d] for d in s.split.validation)
    killed = any(p["killed"] for p in probes)
    check = {
        "checked_at": time.time(), "track": track.name, "history_root": str(history_root),
        "history": history_key(s.history), "test_last": s.split.test[-1], "device": device,
        "probes": probes, "full_train_rows": train_rows, "full_refit_rows": refit_rows,
        "projected_peak": None if killed else project_peak(probes, train_rows),
        "projected_final_peak": None if killed else project_peak(probes, refit_rows),
    }
    Path(work).mkdir(parents=True, exist_ok=True)
    memory_check_path(work, track.name).write_text(json.dumps(check, indent=1))
    return check


def _probe_child(spec: dict) -> dict:
    track = Track(**{**spec["track"], "trials": tuple(spec["track"]["trials"])})
    s = setup(Path(spec["history"]), Path(spec["work"]), spec["inspected"], **spec["split"])
    return probe(s, track, spec["rows"], device=spec["device"], models_dir=Path(spec["models_dir"]),
                 work=Path(spec["work"]) / "probe")


if __name__ == "__main__":
    print(json.dumps(_probe_child(json.loads(sys.argv[1]))), flush=True)
