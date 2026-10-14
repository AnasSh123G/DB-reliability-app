"""Offline, memory-capped neural challengers for the frozen full-year split.

Writes only under this repository's data/ml/compact_training_20261013 tree.
Uses production feature preparation, network classes, loss and decision rules;
the experiment supplies checkpoint selection, streaming metrics and resume.
It never changes CURRENT, the database or inspected dates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import resource
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbrel.ml.arrival import distribution_point, prep_numeric, save_arrival_artifact
from dbrel.ml.arrival_nn import (
    ArrivalNet, BatchSource, NetConfig, UNKNOWN_DROPOUT_COLUMNS, arrival_loss,
    bin_index_torch, check_tensor_sizes, estimate_asset_bytes, export_onnx,
)
from dbrel.ml.features import CATEGORICAL
from dbrel.ml.full_year import PreparedSet
from dbrel.ml.full_year_search import write_browser_check
from dbrel.ml.notice_rates import NoticeRates, WEIGHT
from explore_compact_delay import guard, signature

WORK = ROOT / "data/ml/compact_training_20261013"
PREPARED = ROOT / "data/ml/full_year/prepared"
TRIAL = ROOT / "data/ml/full_year/runs/compact-2026-09-26/trials/f1ac12b1742e3a0b"
PREFIX = "search-2026-09-26-robust-no-doy-mc20-rd56-"
CUT = int(np.datetime64("2026-09-06").astype("datetime64[D]").astype(int))
BLOCK_ROWS = 262144
PREDICT_ROWS = 8192
BUDGET = 36_000_000  # user's flexible experimental limit, not the promotion gate


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def write_json(path, value):
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(value, indent=2))
    partial.replace(path)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cpu_tree(value):
    if isinstance(value, torch.Tensor):
        return value.detach().cpu()
    if isinstance(value, dict):
        return {k: cpu_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(cpu_tree(v) for v in value)
    return value


def save_torch(path, value):
    partial = path.with_suffix(path.suffix + ".partial")
    torch.save(cpu_tree(value), partial)
    partial.replace(path)


class CachedColumn:
    """Contiguous copied reads; tell the kernel it may reclaim consumed pages."""

    def __init__(self, column, start=0, stop=None):
        self.column, self.start = column, start
        self.n = len(column) - start if stop is None else stop-start
        if not 0 <= start <= start+self.n <= len(column):
            raise ValueError("invalid column range")
        self.shape = (self.n, *column.shape[1:])
        self.fd = os.open(column.path, os.O_RDONLY)
        self.row_bytes = column.dtype.itemsize * max(1, column.width)

    def __len__(self):
        return self.n

    def __getitem__(self, index):
        if not isinstance(index, slice) or index.step not in (None, 1):
            raise TypeError("only contiguous slices are supported")
        a, b, _ = index.indices(self.n)
        out = self.column[self.start+a:self.start+b]
        if hasattr(os, "posix_fadvise"):
            try:
                os.posix_fadvise(self.fd, (self.start+a)*self.row_bytes, (b-a)*self.row_bytes,
                                 os.POSIX_FADV_DONTNEED)
            except OSError:
                pass  # a reclamation hint, not required for correct copied reads
        return out

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Source(BatchSource):
    def __init__(self, data, start=0, stop=None):
        self.columns = [CachedColumn(c, start, stop) for c in (data.cats, data.nums, data.y)]
        super().__init__(*self.columns, block_rows=BLOCK_ROWS)

    def close(self):
        for column in self.columns:
            column.close()


class Score:
    def __init__(self):
        self.n = self.scored = 0
        self.total_error = 0.0
        self.hits = dict.fromkeys((1, 3, 5), 0)
        self.band_hits = self.below25 = self.below75 = 0
        self.percentile_rows = 0

    def add(self, y, point, pct=None):
        y = np.asarray(y, dtype=np.float64)
        point = np.asarray(point, dtype=np.float64)
        if not np.isfinite(y).all() or np.isinf(point).any():
            raise ValueError("nonfinite target or infinite point")
        error = np.abs(y-point)
        ok = np.isfinite(error)
        self.n += len(y)
        self.scored += int(ok.sum())
        self.total_error += float(error[ok].sum())
        for tolerance in self.hits:
            self.hits[tolerance] += int(np.count_nonzero(error <= tolerance))
        if pct is not None:
            pct = np.asarray(pct, dtype=np.float64)
            if pct.shape != (len(y), 3) or not np.isfinite(pct).all() or np.any(np.diff(pct, axis=1) < 0):
                raise ValueError("percentiles must be finite and ordered")
            self.percentile_rows += len(y)
            self.band_hits += int(np.count_nonzero((pct[:, 0] <= y) & (y <= pct[:, 2])))
            self.below25 += int(np.count_nonzero(y <= pct[:, 0]))
            self.below75 += int(np.count_nonzero(y <= pct[:, 2]))

    def result(self):
        return {"eligible": self.n, "scored": self.scored, "missing": self.n-self.scored,
                "coverage": self.scored/self.n if self.n else 0,
                "mae_scored": self.total_error/self.scored if self.scored else None,
                **{f"within_{t}_min": count/self.n if self.n else None for t, count in self.hits.items()},
                "quantile_scored": self.percentile_rows,
                "band_coverage": self.band_hits/self.percentile_rows if self.percentile_rows else None,
                "below_p25": self.below25/self.percentile_rows if self.percentile_rows else None,
                "below_p75": self.below75/self.percentile_rows if self.percentile_rows else None}


def selection_rank(score, mae_limit=None):
    """Precision-constrained tolerance accuracy; missing coverage never wins."""
    if score["coverage"] < 1 or score["mae_scored"] is None:
        return (-1, -math.inf, -math.inf)
    mae = score["mae_scored"]
    if mae_limit is None or mae <= mae_limit:
        return (1, score["within_5_min"], -mae)
    return (0, -mae, score["within_5_min"])


def day_boundary(data):
    day = data.column("day")
    lo, hi = 0, len(data)
    while lo < hi:
        mid = (lo+hi)//2
        if int(day[mid:mid+1][0]) < CUT:
            lo = mid+1
        else:
            hi = mid
    if lo <= 0 or lo >= len(data):
        raise ValueError("selection/probe dates are absent")
    # Check the assumption over the small day column in bounded pieces.
    previous = None
    for start in range(0, len(data), BLOCK_ROWS):
        block = day[start:start+BLOCK_ROWS]
        if np.any(np.diff(block) < 0) or (previous is not None and block[0] < previous):
            raise ValueError("prepared validation must be ordered by service date")
        previous = block[-1]
    return lo


def evaluate(net, data, start, stop, device, *, points_path=None, all_rules=False, groups=False):
    names = ["median"] if net.cfg.head == "quantile" else [net.cfg.point_rule]
    if all_rules and net.cfg.head == "binned":
        names = ["median", "window3", "window5"]
    scores = {name: Score() for name in names}
    grouped = {name: {"day": {}, "segment": {}} for name in names}
    points = {} if points_path is None else {
        name: np.lib.format.open_memmap(points_path / f"{name}.npy", mode="w+", dtype="float32", shape=(stop-start,))
        for name in names}
    net.model.to(device).eval()
    source = Source(data, start, stop)
    try:
        with torch.inference_mode():
            for begin in range(0, len(source), PREDICT_ROWS):
                end = min(len(source), begin+PREDICT_ROWS)
                cats, nums, y = source.read(begin, end)
                raw = net._raw(torch.as_tensor(cats, device=device).long(),
                               torch.as_tensor(nums, device=device)).cpu().numpy()
                if not np.isfinite(raw).all():
                    raise ValueError("nonfinite network output")
                for name in names:
                    if net.cfg.head == "quantile":
                        point, pct = raw[:, 1], raw
                    else:
                        rule = "median" if name == "median" else "window"
                        half = 5 if name == "window5" else 3
                        point, pct = distribution_point(raw, net.edges, rule, half)
                    # Production ArrivalForecast stores float32 estimates.
                    point, pct = point.astype(np.float32), pct.astype(np.float32)
                    scores[name].add(y, point, pct)
                    if points:
                        points[name][begin:end] = point
                    if groups:
                        for group, column in (("day", "day"), ("segment", "segment")):
                            values = data.column(column)[start+begin:start+end]
                            for value in np.unique(values):
                                mask = values == value
                                key = str(np.datetime64(int(value), "D")) if group == "day" else str(int(value))
                                grouped[name][group].setdefault(key, Score()).add(y[mask], point[mask], pct[mask])
    finally:
        source.close()
        for array in points.values():
            array.flush()
    return {name: {"overall": score.result(), **({f"by_{group}": {key: s.result() for key, s in values.items()}
                    for group, values in grouped[name].items()} if groups else {})}
            for name, score in scores.items()}


def resources(cgroup, seconds):
    stats = dict(line.split() for line in (cgroup / "memory.stat").read_text().splitlines())
    return {"seconds": seconds, "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            "cgroup_peak_bytes": int((cgroup / "memory.peak").read_text()),
            "cgroup_current_bytes": int((cgroup / "memory.current").read_text()),
            "current_anon_bytes": int(stats["anon"]), "current_file_bytes": int(stats["file"]),
            "memory_max": (cgroup / "memory.max").read_text().strip(),
            "swap_max": (cgroup / "memory.swap.max").read_text().strip(),
            "gpu_peak_allocated_bytes": torch.cuda.max_memory_allocated() if torch.cuda.is_available() else 0,
            "gpu_peak_reserved_bytes": torch.cuda.max_memory_reserved() if torch.cuda.is_available() else 0}


def configuration(name):
    base = NetConfig(**json.loads((TRIAL / "trial.json").read_text())["params"]["config"])
    if name == "fine":
        return replace(base, lr=3e-4, epochs=6, patience=6, seed=0), True, True
    if name == "wide":
        return replace(base, hidden=1024, epochs=12, patience=12, seed=1), False, True
    if name == "distribution":
        return replace(base, head="binned", point_rule="window", epochs=12, patience=12, seed=1), False, False
    raise ValueError(name)


def reference_score(data, boundary):
    with np.load(TRIAL / "validation.npz") as saved:
        ids, point = saved["stop_id"], saved["point"]
    if len(point) != len(data):
        raise ValueError("reference row count differs")
    scores = {"selection": Score(), "probe": Score()}
    for start in range(0, len(data), BLOCK_ROWS):
        stop = min(len(data), start+BLOCK_ROWS)
        if not np.array_equal(ids[start:stop], data.column("stop_id")[start:stop]):
            raise ValueError("reference arrival IDs differ")
        y = data.y[start:stop]
        a = min(stop, boundary)-start
        if a > 0:
            scores["selection"].add(y[:a], point[start:start+a])
        a = max(start, boundary)-start
        if a < len(y):
            scores["probe"].add(y[a:], point[start+a:stop])
    return {role: score.result() for role, score in scores.items()}


def input_file_identity(data):
    return {name: {"bytes": (data.dir / f"{name}.bin").stat().st_size,
                   "mtime_ns": (data.dir / f"{name}.bin").stat().st_mtime_ns}
            for name in ("cats", "nums", "y", "day", "stop_id", "segment")}


def notice_record(fit):
    """Recover and verify exactly the rates the immutable prepared cache used."""
    from sqlalchemy import create_engine
    dates = json.loads((TRIAL.parents[1] / "key.json").read_text())["split"]["train"]
    if signature(tuple(dates)) != fit.key["rows"]["dates_sha"]:
        raise ValueError("training dates differ from the notice-rate record")
    record = {"dates": dates, "weight": WEIGHT, "fingerprint": fit.key["notice_rates"]["fingerprint"]}
    database = ROOT / "data/dbrel.sqlite"
    # The SQLite URI enforces read-only access, including against accidental SQL.
    engine = create_engine(f"sqlite+pysqlite:///file:{database}?mode=ro&uri=true", future=True)
    try:
        rates = NoticeRates.from_record(engine, record)
        return rates.record()
    finally:
        engine.dispose()


def artifact_export(root, args, inputs, cfg, result, check, fit):
    notice = notice_record(fit)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(), "source": {"experiment": str(root.relative_to(ROOT))},
        "config": cfg.as_dict(), "prep": inputs["prep"], "vocab": inputs["vocab"],
        "output": check["output"], "edges": None if cfg.head == "quantile" else net_edges(),
        "point_rule": cfg.point_rule, "features": list(CATEGORICAL)+list(prep_numeric(inputs["prep"])),
        "notice_rates": notice, "scores": {"selection": result["selected_epoch"], "later_validation": result["later_probe"]},
        "disclosure": ["existing validation reused; not an independent test", "historical five-hour-cutoff replay",
                       "present-day timetable patterns; historical timetable-as-of not verified"],
        "onnx_check": result["onnx"], "experimental_budget_bytes": BUDGET,
    }
    # Budget the same complete shape save_arrival_artifact will serialize.
    preview = {**manifest, "kind": "arrival-net", "model_file": "model.onnx", "sides": ["arr"],
               "window_half_min": 3.0, "asset_bytes": result["onnx_bytes"], "role": "deployable",
               "budget_bytes": 35_000_000, "download_bytes": 0}
    for _ in range(2):
        preview["download_bytes"] = result["onnx_bytes"] + len(json.dumps(preview, indent=1).encode())
    role = "deployable" if preview["download_bytes"] <= 35_000_000 else "comparison"
    models = root / "artifacts"
    models.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = save_arrival_artifact(models, manifest, {"model.onnx": root / "onnx/model.onnx"}, stamp,
                                     role=role, served_dir=ROOT / "models/v2")
    measured = json.loads((artifact / "manifest.json").read_text())["download_bytes"]
    if measured > BUDGET:
        raise ValueError(f"complete artifact is {measured:,} bytes, above experimental size limit")
    return {"path": str(artifact.relative_to(ROOT)), "download_bytes": measured, "role": role,
            "fits_strict_35mb": measured <= 35_000_000}


def net_edges():
    from dbrel.ml.arrival import bin_edges
    return bin_edges().tolist()


def training(net, source, val_data, val_stop, device, directory, key, mae_limit):
    cfg, model = net.cfg, net.model.to(device)
    check_tensor_sizes(cfg, net.n_bins, source.n_num, net.d_in(), source.block_rows)
    rng = np.random.default_rng(cfg.seed)
    decay = [p for name, p in model.named_parameters() if p.ndim == 2 and not name.startswith("inputs.")]
    other = [p for name, p in model.named_parameters() if not (p.ndim == 2 and not name.startswith("inputs."))]
    opt = torch.optim.AdamW([{"params": decay, "weight_decay": cfg.weight_decay},
                             {"params": other, "weight_decay": 0}], lr=cfg.lr)
    steps = sum(math.ceil((b-a)/cfg.batch_size) for a, b in source.spans())
    scheduler = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=cfg.lr, total_steps=steps*cfg.epochs)
    edges = torch.as_tensor(net.edges, device=device, dtype=torch.float32)
    dropout_columns = torch.tensor([CATEGORICAL.index(name) for name in UNKNOWN_DROPOUT_COLUMNS], device=device)
    best_rank, best_epoch, best_file, history, start_epoch = None, None, None, [], 0
    latest = directory / "latest.pt"
    if latest.exists():
        # This trusted checkpoint was written by this script in this repo;
        # optimizer/RNG state requires more than a weights-only load.
        state = torch.load(latest, map_location="cpu", weights_only=False)
        if state["key"] != key:
            raise ValueError("checkpoint setup changed; use a new run directory")
        model.load_state_dict(state["model"]); opt.load_state_dict(state["optimizer"])
        scheduler.load_state_dict(state["scheduler"])
        rng.bit_generator.state = state["numpy_rng"]
        torch.set_rng_state(state["torch_rng"])
        if device != "cpu":
            torch.cuda.set_rng_state_all(state["cuda_rng"])
        best_rank, best_epoch, history, start_epoch = state["best_rank"], state["best_epoch"], state["history"], state["epoch"]
        best_file = state["best_file"]
        del state
        log(f"resume after epoch {start_epoch}")
    on_gpu = device != "cpu"
    for epoch in range(start_epoch, cfg.epochs):
        model.train()
        began = time.monotonic()
        total = torch.zeros((), device=device)
        n_batches = 0
        for block_index, (cats, nums, y) in enumerate(source.blocks(rng)):
            if not np.isfinite(y).all():
                raise ValueError("training target must be finite")
            cats_t = torch.as_tensor(cats, device=device).long()
            nums_t = torch.as_tensor(nums, device=device)
            y_t = torch.as_tensor(y, device=device)
            y_bin = bin_index_torch(y_t, edges)
            order = torch.randperm(len(y), device=device)
            for begin in range(0, len(y), cfg.batch_size):
                idx = order[begin:begin+cfg.batch_size]
                cat = cats_t[idx]
                if cfg.unknown_rate:
                    unknown = torch.rand(len(idx), len(dropout_columns), device=device) < cfg.unknown_rate
                    cat[:, dropout_columns] = torch.where(unknown, 0, cat[:, dropout_columns])
                with torch.autocast("cuda", dtype=torch.bfloat16, enabled=on_gpu):
                    raw = model(cat, nums_t[idx])
                loss = arrival_loss(cfg, raw.float(), y_t[idx], y_bin[idx])
                if not torch.isfinite(loss):
                    raise ValueError("nonfinite training loss")
                opt.zero_grad(set_to_none=True); loss.backward(); opt.step(); scheduler.step()
                total += loss.detach(); n_batches += 1
            del cats_t, nums_t, y_t, y_bin, order, raw, loss, cat
            if (block_index+1) % 100 == 0:
                log(f"epoch {epoch+1}: {min((block_index+1)*BLOCK_ROWS, len(source)):,}/{len(source):,} rows")
        selected = evaluate(net, val_data, 0, val_stop, device)
        score = next(iter(selected.values()))["overall"]
        rank = selection_rank(score, mae_limit)
        entry = {"epoch": epoch+1, "train_loss": float(total)/n_batches, "selection": score,
                 "seconds": time.monotonic()-began}
        history.append(entry)
        if best_rank is None or rank > best_rank:
            best_rank, best_epoch = rank, epoch+1
            best_file = f"epoch-{epoch+1:03d}.pt"
            save_torch(directory / best_file, model.state_dict())
        write_json(directory / "history.json", {"epochs": history, "best_epoch": best_epoch,
                                               "precision_limit": mae_limit})
        save_torch(latest, {"key": key, "epoch": epoch+1, "model": model.state_dict(),
                           "optimizer": opt.state_dict(), "scheduler": scheduler.state_dict(),
                           "numpy_rng": rng.bit_generator.state, "torch_rng": torch.get_rng_state(),
                           "cuda_rng": torch.cuda.get_rng_state_all() if on_gpu else [],
                           "best_rank": best_rank, "best_epoch": best_epoch, "best_file": best_file, "history": history})
        log(f"epoch {epoch+1}: {json.dumps(entry)}; best={best_epoch}")
    model.load_state_dict(torch.load(directory / best_file, map_location=device, weights_only=True))
    save_torch(directory / "best.pt", model.state_dict())
    return history, best_epoch


def run(args, cgroup):
    started = time.monotonic()
    fit, val = PreparedSet(PREPARED / f"{PREFIX}fit"), PreparedSet(PREPARED / f"{PREFIX}eval")
    inputs = json.loads((TRIAL / "inputs.json").read_text())
    for data in (fit, val):
        if signature(inputs["prep"]) != data.key["prep"] or signature(inputs["vocab"]) != data.key["vocab"]:
            raise ValueError("prepared input fingerprint differs from trial")
    if fit.key["rows"]["last"] >= val.key["rows"]["first"] or fit.key["rows"]["finalized_before"] is None:
        raise ValueError("training split is not chronological and finalized before selection")
    boundary = day_boundary(val)
    reference = reference_score(val, boundary)
    cfg, warm, constrained = configuration(args.track)
    if args.command == "probe":
        cfg = replace(cfg, epochs=1)
    script_sha = file_hash(Path(__file__))
    root = WORK / (f"probes/{args.track}-{args.rows}-{script_sha[:8]}" if args.command == "probe" else f"runs/{args.track}")
    root.mkdir(parents=True, exist_ok=True)
    key = {"version": 2, "script_sha": script_sha, "config": cfg.as_dict(),
           "device": args.device, "torch_version": str(torch.__version__),
           "hip_version": torch.version.hip, "cuda_version": torch.version.cuda,
           "fit": fit.key, "fit_metadata": fit.meta, "validation": val.key, "validation_metadata": val.meta,
           "selection_end_exclusive": "2026-09-06", "warm_start_sha": file_hash(TRIAL / "model.pt") if warm else None,
           "mode": args.command, "probe_rows": args.rows if args.command == "probe" else None,
           "batch_block_rows": BLOCK_ROWS, "selection": "within5_precision" if constrained else "within5",
           "input_sha": file_hash(TRIAL / "inputs.json"), "reference_predictions_sha": file_hash(TRIAL / "validation.npz"),
           "precision_limit": reference["selection"]["mae_scored"] if constrained else None,
           "production_code": {str(path.relative_to(ROOT)): file_hash(path) for path in (
                ROOT / "src/dbrel/ml/arrival_nn.py", ROOT / "src/dbrel/ml/arrival.py",
                ROOT / "src/dbrel/ml/features.py", ROOT / "src/dbrel/ml/full_year.py",
                ROOT / "src/dbrel/ml/notice_rates.py")},
           "fit_files": input_file_identity(fit), "validation_files": input_file_identity(val)}
    if (root / "key.json").exists() and json.loads((root / "key.json").read_text()) != key:
        raise ValueError("run setup changed; choose a fresh experiment root")
    write_json(root / "key.json", key)
    if (root / "result.json").exists():
        log(f"already completed: {root}"); return
    if args.device != "cpu":
        if not torch.cuda.is_available():
            raise ValueError("requested GPU is unavailable")
        torch.cuda.set_per_process_memory_fraction(0.60)
        torch.cuda.reset_peak_memory_stats()
    torch.set_num_threads(4)
    net = ArrivalNet(cfg, inputs["prep"])
    estimate = estimate_asset_bytes(cfg, inputs["prep"], inputs["vocab"])
    if estimate > BUDGET:
        raise ValueError(f"estimated asset {estimate:,} exceeds {BUDGET:,} experimental budget")
    if warm:
        net.model.load_state_dict(torch.load(TRIAL / "model.pt", map_location="cpu", weights_only=True))
    train_rows = min(args.rows, len(fit)) if args.command == "probe" else len(fit)
    val_stop = min(200000, boundary) if args.command == "probe" else boundary
    precision_limit = reference["selection"]["mae_scored"] if constrained else None
    source = Source(fit, 0, train_rows)
    try:
        log(f"{args.track}: {train_rows:,} training rows, {val_stop:,} selection rows, estimated {estimate/1e6:.2f} MB")
        history, best_epoch = training(net, source, val, val_stop, args.device, root, key, precision_limit)
    finally:
        source.close()
    result = {"scope": "existing validation, not fresh test", "track": args.track, "config": cfg.as_dict(),
              "estimated_asset_bytes": estimate, "training_rows": train_rows, "selection_rows": val_stop,
              "best_epoch": best_epoch, "reference": reference, "selected_epoch": history[best_epoch-1]["selection"]}
    if args.command == "train":
        result["later_probe"] = evaluate(net, val, boundary, len(val), args.device,
                                          points_path=root, all_rules=True, groups=True)
        # Uniformly spaced selection rows, plus valid UNKNOWN category slots.
        index = np.linspace(0, boundary-1, 512, dtype=int)
        cats = np.concatenate([val.cats[int(i):int(i)+1] for i in index])
        nums = np.concatenate([val.nums[int(i):int(i)+1] for i in index])
        cats = np.concatenate([cats, np.zeros_like(cats[:64])])
        nums = np.concatenate([nums, nums[:64]])
        write_json(root / "metrics.json", result)
        # Keep an export failure recoverable from the completed checkpoint.
        # Old exports are retained under a local timestamp, never overwritten.
        if (root / "onnx").exists():
            (root / "onnx").rename(root / f"onnx-previous-{time.time_ns()}")
        try:
            check = export_onnx(net, cats, nums, root / "onnx")
        except Exception as error:
            write_json(root / "export_failure.json", {"reason": str(error), "kind": type(error).__name__})
            raise
        result["onnx"] = {k: v for k, v in check.items() if k != "_expected"}
        result["onnx_bytes"] = (root / "onnx/model.onnx").stat().st_size
        (root / "inputs.json").write_text(json.dumps(inputs, indent=1))
        result["onnx_plus_inputs_bytes"] = result["onnx_bytes"] + (root / "inputs.json").stat().st_size
        result["within_experimental_budget"] = result["onnx_plus_inputs_bytes"] <= BUDGET
        if not check["onnx_passed"] or not result["within_experimental_budget"]:
            write_json(root / "export_failure.json", {"onnx": result["onnx"],
                       "onnx_plus_inputs_bytes": result["onnx_plus_inputs_bytes"], "budget": BUDGET})
            raise ValueError("candidate export failed numerical parity or size gate; metrics and checkpoint retained")
        write_browser_check(root / "browser_check", args.track, check, root / "onnx", cats, nums)
        result["artifact"] = artifact_export(root, args, inputs, cfg, result, check, fit)
        if (root / "export_failure.json").exists():
            (root / "export_failure.json").unlink()
    result["resources"] = resources(cgroup, time.monotonic()-started)
    write_json(root / "result.json", result)
    log(f"finished: {root}; {json.dumps(result['resources'])}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("probe", "train"))
    parser.add_argument("--track", required=True, choices=("fine", "wide", "distribution"))
    parser.add_argument("--rows", type=int, default=500000)
    parser.add_argument("--device", default="cuda", choices=("cuda", "cpu"))
    args = parser.parse_args()
    if not 100000 <= args.rows <= 2000000:
        parser.error("probe rows must be between 100,000 and 2,000,000")
    # Ensure every experiment write is local, even if a directory was rebound.
    if ROOT.resolve() not in WORK.resolve().parents:
        raise SystemExit("experiment root must stay inside this repository")
    run(args, guard())


if __name__ == "__main__":
    main()
