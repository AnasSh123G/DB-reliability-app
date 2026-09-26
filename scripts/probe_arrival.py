"""Correctness and resource probes for the arrival-only candidates (before any full run).

Run as a capped job with the PyTorch-ROCm venv:

    dbrel ml launch arrival-probe --memory 14G -- \\
        env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/probe_arrival.py

Networks (each NetConfig in `CONFIGS`):
- CPU vs GPU parity: identical weights, one batch, eval mode, fp32 -- forward
  outputs, loss and gradients must agree within `TOLERANCE`. The bf16 autocast
  forward used in training is compared too (recorded, not gated).
- One training epoch on a date-stratified training subset on the GPU:
  throughput, VRAM and host peaks, and an estimated full-fit time.

Histogram: fitted on the full training history (timed, memory measured), and a
sample of validation predictions recomputed by brute force from the matching
training arrivals.

No accuracy is computed: validation outcomes are read only by the network's
early-stopping bookkeeping and are not recorded. Writes a new
data/ml/experiments/arrival_probe-<UTC>.json; exits 1 if any check fails.
"""

from __future__ import annotations

import json
import platform
import resource
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbrel.ml.arrival import (  # noqa: E402
    ConditionalHistogram, _component, arrival_inputs, bin_edges, bin_index, fit_input_prep,
    grouped_quantiles,
)
from dbrel.ml.dataset import load_arrival_dataset  # noqa: E402
from dbrel.ml.memory import GIB, memory_report  # noqa: E402
from dbrel.ml.split import arrival_experiment_masks, restrict_to_training_vocab  # noqa: E402

TRAINING = [f"2026-08-{d}" for d in range(24, 32)] + [f"2026-09-{d:02d}" for d in range(1, 13)]
VALIDATION = [f"2026-09-{d}" for d in range(13, 17)]
INSPECTED = [f"2026-09-{d}" for d in range(17, 23)]
TOLERANCE = {"loss_relative": 1e-4, "grad_relative": 1e-3, "forward_max_abs": 1e-3}
PARITY_ROWS = 8192
SUBSET_PER_DATE = 20_000
VAL_SUBSET = 50_000
HIST_CHECK_ROWS = 300
MIN_SUPPORT = 10


def _configs(NetConfig):
    return {
        "tabm-quantile": NetConfig(arch="tabm", head="quantile", point_rule="median"),
        "tabm-binned": NetConfig(arch="tabm", head="binned", point_rule="window"),
        "mlp-quantile": NetConfig(arch="mlp", head="quantile", point_rule="median"),
        "mlp-binned": NetConfig(arch="mlp", head="binned", point_rule="window"),
    }


def _host_peak() -> dict:
    report = memory_report()
    return {"rss_gib": report["rss"] / GIB, "swap_gib": report["swap"] / GIB,
            "group_peak_gib": None if report["group_peak"] is None else report["group_peak"] / GIB,
            "process_max_rss_gib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / GIB}


def _stratified(rows_by_day: np.ndarray, day: np.ndarray, per_date: int, rng) -> np.ndarray:
    picked = []
    for d in np.unique(day[rows_by_day]):
        rows = rows_by_day[day[rows_by_day] == d]
        picked.append(rng.choice(rows, min(per_date, len(rows)), replace=False))
    return np.sort(np.concatenate(picked))


def parity(torch, ArrivalNet, arrival_loss, cfg, prep, cats, nums, y, edges, rows) -> dict:
    ref = ArrivalNet(cfg, prep, edges)
    state = {k: v.clone() for k, v in ref.model.state_dict().items()}
    out = {}
    for device in ("cpu", "cuda"):
        net = ArrivalNet(cfg, prep, edges)
        net.model.load_state_dict(state)
        model = net.model.to(device).eval()
        c = torch.as_tensor(cats[rows], device=device)
        n = torch.as_tensor(nums[rows], device=device)
        t = torch.as_tensor(y[rows].astype(np.float32), device=device)
        tb = torch.as_tensor(bin_index(y[rows], edges), device=device)
        raw = model(c, n)
        loss = arrival_loss(cfg, raw, t, tb)
        loss.backward()
        grad = torch.cat([p.grad.flatten() for p in model.parameters() if p.grad is not None])
        out[device] = (float(loss.detach()), grad.cpu().double(), raw.detach().cpu().double())
        if device == "cuda":
            with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
                bf16 = model(c, n).float()
            bf16_loss = float(arrival_loss(cfg, bf16, t, tb))
    loss_rel = abs(out["cuda"][0] - out["cpu"][0]) / abs(out["cpu"][0])
    grad_rel = float((out["cuda"][1] - out["cpu"][1]).norm() / out["cpu"][1].norm())
    fwd = float((out["cuda"][2] - out["cpu"][2]).abs().max())
    return {
        "rows": len(rows), "loss_cpu": out["cpu"][0], "loss_gpu": out["cuda"][0],
        "loss_relative_difference": loss_rel, "grad_relative_difference": grad_rel,
        "forward_max_abs_difference": fwd,
        "bf16_loss_relative_difference": abs(bf16_loss - out["cuda"][0]) / abs(out["cuda"][0]),
        "passed": bool(loss_rel < TOLERANCE["loss_relative"] and grad_rel < TOLERANCE["grad_relative"]
                       and fwd < TOLERANCE["forward_max_abs"]),
    }


def train_probe(torch, ArrivalNet, cfg, prep, cats, nums, y, edges, train_rows, val_rows,
                full_train_rows: int) -> dict:
    from dataclasses import replace

    cfg1 = replace(cfg, epochs=1, patience=1)
    net = ArrivalNet(cfg1, prep, edges)
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    history = net.fit(cats, nums, y, train_rows, val_rows, device="cuda")
    torch.cuda.synchronize()
    epoch_s = time.monotonic() - started
    t = time.monotonic()
    net.predict_arrays(cats[val_rows], nums[val_rows], device="cuda")
    torch.cuda.synchronize()
    infer_s = time.monotonic() - t
    train_s = max(epoch_s - infer_s, 1e-9)  # the epoch includes one validation prediction
    per_row = train_s / len(train_rows)
    return {
        "config": cfg.as_dict(), "n_parameters": net.n_parameters(),
        "fp32_weight_bytes": 4 * net.n_parameters(),
        "train_rows": len(train_rows), "val_rows": len(val_rows),
        "epoch_seconds": round(epoch_s, 2), "train_rows_per_second": round(len(train_rows) / train_s),
        "inference_rows_per_second": round(len(val_rows) / infer_s),
        "train_loss": history[0]["train_loss"],
        "vram_peak_allocated_gib": torch.cuda.max_memory_allocated() / GIB,
        "vram_peak_reserved_gib": torch.cuda.max_memory_reserved() / GIB,
        "host": _host_peak(),
        "estimated_full_epoch_minutes": round(per_row * full_train_rows / 60, 1),
        "estimated_full_fit_minutes_at_max_epochs": round(per_row * full_train_rows * cfg.epochs / 60, 1),
    }


def _brute_window(v: np.ndarray, half: float = 3.0) -> float:
    covered = np.searchsorted(v, v + 2 * half + 1e-6, side="right") - np.arange(len(v))
    i = int(np.argmax(covered))
    return float((v[i] + v[i + covered[i] - 1]) / 2)


def histogram_probe(enc, y, train_mask, val_rows, rng) -> dict:
    started = time.monotonic()
    model = ConditionalHistogram(min_support=MIN_SUPPORT, point_rule="window").fit(enc, y, train_mask)
    fit_s = time.monotonic() - started
    host_after_fit = _host_peak()
    t = time.monotonic()
    out = model.predict({k: v[val_rows] for k, v in enc.items()})
    predict_s = time.monotonic() - t
    levels, counts = np.unique(out.detail["level"], return_counts=True)

    train_rows = np.flatnonzero(train_mask)
    comps = {c: _component(enc, c) for lv in model.levels for c in lv.columns}
    failures = []
    sample = rng.choice(len(val_rows), min(HIST_CHECK_ROWS, len(val_rows)), replace=False)
    for i in sample:
        row = val_rows[i]
        name = out.detail["level"][i]
        chosen = [lv.name for lv in model.levels].index(name)
        for li, lv in enumerate(model.levels[:chosen + 1]):
            match = np.ones(len(train_rows), bool)
            for c in lv.columns:
                match &= comps[c][train_rows] == comps[c][row]
            known = all(comps[c][row] > 0 for c in lv.columns)
            n = int(match.sum()) if known else 0
            if li < chosen and n >= MIN_SUPPORT and li != len(model.levels) - 1:
                failures.append({"row": int(row), "problem": f"skipped {lv.name} with support {n}"})
        v = np.sort(y[train_rows[match]].astype(np.float64))
        pct = grouped_quantiles(v, np.array([0]), np.array([len(v)]))[0]
        expect = {"support": len(v), "point": _brute_window(v), "pct": pct.tolist()}
        got = {"support": int(out.detail["support"][i]), "point": float(out.point[i]),
               "pct": out.percentiles[i].tolist()}
        if (expect["support"] != got["support"] or abs(expect["point"] - got["point"]) > 1e-4
                or not np.allclose(expect["pct"], got["pct"], atol=1e-4)):
            failures.append({"row": int(row), "level": name, "expected": expect, "got": got})
    return {
        "min_support": MIN_SUPPORT, "train_rows": len(train_rows), "fit_seconds": round(fit_s, 1),
        "predict_rows_per_second": round(len(val_rows) / predict_s),
        "host_after_fit": host_after_fit,
        "estimated_asset_bytes": model.asset_bytes(),
        "keys_per_level": {lv.name: len(t.keys) for lv, t in zip(model.levels, model.tables)},
        "validation_level_usage": {str(k): int(c) for k, c in zip(levels, counts)},
        "missing": int(np.count_nonzero(out.missing_reason != "")),
        "brute_force_rows_checked": len(sample), "brute_force_failures": failures[:20],
        "passed": not failures,
    }


def main() -> int:
    import tabm
    import torch

    from dbrel.ml.arrival_nn import ArrivalNet, NetConfig, arrival_loss

    torch.set_num_threads(4)
    rng = np.random.default_rng(0)
    started = time.monotonic()
    ds = load_arrival_dataset(ROOT / "data" / "ml")
    split, masks = arrival_experiment_masks(ds, training=TRAINING, validation=VALIDATION,
                                            inspected=INSPECTED)
    enc, vocab = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    y = ds.arr_min.astype(np.float64)
    props = torch.cuda.get_device_properties(0)
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "versions": {"python": platform.python_version(), "torch": torch.__version__,
                     "torch_hip": torch.version.hip, "tabm": getattr(tabm, "__version__", "?"),
                     "numpy": np.__version__, "kernel": platform.release(),
                     "device": torch.cuda.get_device_name(0), "arch": props.gcnArchName,
                     "vram_total_gib": props.total_memory / GIB},
        "dataset": {"built_at": ds.meta["built_at"], "rows": len(y),
                    "train_rows_finalized_before_validation": int(masks["train"].sum()),
                    "validation_rows": int(masks["validation"].sum())},
        "split": split.as_dict(), "tolerance": TOLERANCE,
        "note": "resource/correctness probe only; no accuracy recorded",
    }
    print(f"loaded {len(y):,} arrivals, {result['dataset']['train_rows_finalized_before_validation']:,} "
          f"training ({time.monotonic() - started:.0f}s)", flush=True)

    result["histogram"] = histogram_probe(enc, y, masks["train"], np.flatnonzero(masks["validation"]), rng)
    print(f"histogram: {json.dumps({k: v for k, v in result['histogram'].items() if k != 'brute_force_failures'})}",
          flush=True)

    edges = bin_edges()
    train_all = np.flatnonzero(masks["train"])
    sub_train = _stratified(train_all, ds.day, SUBSET_PER_DATE, rng)
    val_all = np.flatnonzero(masks["validation"])
    sub_val = np.sort(rng.choice(val_all, min(VAL_SUBSET, len(val_all)), replace=False))
    parity_rows = rng.choice(sub_train, PARITY_ROWS, replace=False)
    result["networks"] = {}
    preps = {}
    for name, cfg in _configs(NetConfig).items():
        scaling = "robust" if cfg.arch == "mlp" else "standard"
        if scaling not in preps:
            preps[scaling] = fit_input_prep(enc, masks["train"], vocab, scaling)
            cats, nums = arrival_inputs(enc, preps[scaling])
            preps[scaling] = (preps[scaling], cats, nums)
        prep, cats, nums = preps[scaling]
        entry = {"parity": parity(torch, ArrivalNet, arrival_loss, cfg, prep, cats, nums, y, edges,
                                  parity_rows)}
        print(f"{name} parity: {entry['parity']}", flush=True)
        if entry["parity"]["passed"]:
            entry["training"] = train_probe(torch, ArrivalNet, cfg, prep, cats, nums, y, edges,
                                            sub_train, sub_val, len(train_all))
            print(f"{name} training: {entry['training']}", flush=True)
        result["networks"][name] = entry
        torch.cuda.empty_cache()

    result["host_final"] = _host_peak()
    result["seconds"] = round(time.monotonic() - started, 1)
    result["passed"] = bool(result["histogram"]["passed"]
                            and all(e["parity"]["passed"] for e in result["networks"].values()))
    out = ROOT / "data" / "ml" / "experiments"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"arrival_probe-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=1, default=float))
    print(f"{'PASSED' if result['passed'] else 'FAILED'} in {result['seconds'] / 60:.1f} min -> {path}",
          flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
