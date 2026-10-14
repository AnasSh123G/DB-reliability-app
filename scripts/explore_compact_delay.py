"""Bounded, offline probes of compact delay models; never writes served artifacts.

Run each command in its own systemd user service with MemoryMax=8G and
MemorySwapMax=0. Reuses the saved full-year split and prepared inputs read-only.
The second half of the old validation period is a diagnostic probe, not a
new test set: the original search already inspected the whole validation set.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbrel.ml.features import CATEGORICAL
from dbrel.ml.full_year import PreparedSet
from dbrel.ml.metrics import score_arrivals

PREFIX = "search-2026-09-26-robust-"
WINNER = "f1ac12b1742e3a0b"
BINNED = "2e7bffd7a918584d"
NOTICE = "634b5e057ab5fe00"
BLOCK = 65536


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def guard():
    cg = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
              if line.startswith("0::"))
    directory = Path("/sys/fs/cgroup") / cg.lstrip("/")
    cap = (directory / "memory.max").read_text().strip()
    swap = (directory / "memory.swap.max").read_text().strip()
    if cap == "max" or int(cap) > 8 * 2**30 or swap != "0":
        raise SystemExit("Run inside a private cgroup with MemoryMax<=8G and MemorySwapMax=0")
    return directory


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def prepared(args, numeric, role):
    return PreparedSet(args.prepared / f"{PREFIX}{numeric}-mc20-rd56-{role}")


def trial_inputs(args, tid, data):
    inputs = json.loads((args.trials / tid / "inputs.json").read_text())
    if signature(inputs["prep"]) != data.key["prep"] or signature(inputs["vocab"]) != data.key["vocab"]:
        raise ValueError("trial and prepared input fingerprints differ")
    return inputs


def stored_points(args, tid, data):
    trial_inputs(args, tid, data)
    with np.load(args.trials / tid / "validation.npz") as z:
        ids = z["stop_id"]
        for start in range(0, len(data), BLOCK):
            if not np.array_equal(ids[start:start+BLOCK], data.column("stop_id")[start:start+BLOCK]):
                raise ValueError("saved prediction rows differ from prepared arrivals")
        return z["point"]


def report(data, points):
    # Read only small scalar columns; never materialize the full feature matrix.
    y = np.asarray(data.y)
    day = np.asarray(data.column("day"))
    segment = np.asarray(data.column("segment"))
    cutoff = np.datetime64("2026-09-06").astype("datetime64[D]").astype(int)
    result = {}
    for name, point in points.items():
        result[name] = {"overall": score_arrivals(y, point),
                        "calibration": score_arrivals(y[day < cutoff], point[day < cutoff]),
                        "probe": score_arrivals(y[day >= cutoff], point[day >= cutoff]),
                        "by_segment": {str(s): score_arrivals(y[segment == s], point[segment == s])
                                       for s in np.unique(segment)},
                        "by_day": {str(np.datetime64(int(d), "D")): score_arrivals(y[day == d], point[day == d])
                                   for d in np.unique(day)}}
        print(name, json.dumps(result[name]["probe"]), flush=True)
    return result


def screen(args):
    import torch
    from dbrel.ml.arrival import distribution_point
    from dbrel.ml.arrival_nn import ArrivalNet, NetConfig, export_onnx
    from dbrel.ml.full_year_search import write_browser_check

    torch.set_num_threads(4)
    data = prepared(args, "no-doy", "eval")
    base = stored_points(args, WINNER, data)
    points = {"mlp_median": base}
    for shift in (1.0, 2.0, 3.0):
        points[f"median_plus_{shift:g}_diagnostic"] = base + shift
    for value in (0.0, 2.0, 4.0):
        points[f"constant_{value:g}_diagnostic"] = np.full(len(data), value, np.float32)
    notice_data = prepared(args, "with-notice", "eval")
    second = stored_points(args, NOTICE, notice_data)
    for weight in (0.25, 0.5, 0.75):
        points[f"ensemble_no_doy_weight_{weight}"] = base * weight + second * (1-weight)

    inputs = trial_inputs(args, BINNED, notice_data)
    cfg = NetConfig(**json.loads((args.trials / BINNED / "trial.json").read_text())["params"]["config"])
    net = ArrivalNet(cfg, inputs["prep"])
    net.model.load_state_dict(torch.load(args.trials / BINNED / "model.pt", map_location="cpu", weights_only=True))
    net.model.to(args.device).eval()
    for name in ("binned_median", "binned_window3", "binned_window5"):
        points[name] = np.lib.format.open_memmap(args.out / f"{name}.npy", mode="w+", dtype="float32", shape=(len(data),))
    with torch.inference_mode():
        for start in range(0, len(data), 8192):
            stop = min(len(data), start+8192)
            cats = torch.as_tensor(notice_data.cats[start:stop], dtype=torch.int64, device=args.device)
            nums = torch.as_tensor(notice_data.nums[start:stop], device=args.device)
            probs = net._raw(cats, nums).cpu().numpy()
            for name, rule, half in (("binned_median", "median", 3), ("binned_window3", "window", 3),
                                     ("binned_window5", "window", 5)):
                points[name][start:stop] = distribution_point(probs, net.edges, rule, half)[0]
            if start % (8192*100) == 0:
                print(f"binned: {stop:,}/{len(data):,}", flush=True)
    result = report(data, points)
    chosen = max(result, key=lambda k: (result[k]["calibration"]["within_5_min"],
                                       -result[k]["calibration"]["mae_scored"]))
    write(args.out / "screen.json", {"scope": "old validation only; no fresh test claim", "scores": result,
                                    "chosen_on_first_14_days": chosen, "split": data.key["rows"]})
    check = export_onnx(net, notice_data.cats[:1000], notice_data.nums[:1000], args.out / "binned_onnx")
    write_browser_check(args.out / "browser_check", "binned", check, args.out / "binned_onnx",
                        notice_data.cats[:1000], notice_data.nums[:1000])
    print("ONNX", json.dumps({k: v for k, v in check.items() if k != "_expected"}), flush=True)


def sample(data, rows, seed):
    # Sample across the whole chronological training period. Read contiguous
    # blocks, then subselect, so mmap page residency never grows with 89M rows.
    rng = np.random.default_rng(seed)
    sizes = [min(BLOCK, len(data)-s) for s in range(0, len(data), BLOCK)]
    allocation = rng.multinomial(rows, np.asarray(sizes)/len(data))
    X = np.empty((rows, len(CATEGORICAL)+data.meta["n_num"]), np.float32)
    y = np.empty(rows, np.float32)
    cursor = 0
    for start, size, count in zip(range(0, len(data), BLOCK), sizes, allocation):
        if not count:
            continue
        index = rng.choice(size, size=count, replace=False)
        end = cursor + count
        X[cursor:end, :len(CATEGORICAL)] = data.cats[start:start+size][index]
        X[cursor:end, len(CATEGORICAL):] = data.nums[start:start+size][index]
        y[cursor:end] = data.y[start:start+size][index]
        cursor = end
    return X, y


def trees(args):
    import lightgbm as lgb
    data = prepared(args, "no-doy", "fit")
    eval_data = prepared(args, "no-doy", "eval")
    X, y = sample(data, args.sample_rows, 20261013)
    # Select rounds on first 14 validation days, evaluate the later 14 separately.
    days = np.asarray(eval_data.column("day"))
    cut = np.datetime64("2026-09-06").astype("datetime64[D]").astype(int)
    if np.any(np.diff(days) < 0):
        raise ValueError("validation rows must be chronological")
    stop = int(np.searchsorted(days, cut))
    calibration_X, calibration_y = sample(PreparedSlice(eval_data, stop), 150000, 123)
    train = lgb.Dataset(X, y, categorical_feature=list(range(len(CATEGORICAL))), free_raw_data=True)
    valid = lgb.Dataset(calibration_X, calibration_y, reference=train)
    all_predictions = np.empty((len(eval_data), 3), np.float32)
    metadata = []
    for j, alpha in enumerate((0.25, 0.5, 0.75)):
        print(f"LightGBM alpha={alpha}, {len(y):,} training rows", flush=True)
        model = lgb.train({"objective": "quantile", "alpha": alpha, "metric": "quantile", "num_leaves": 63,
                           "learning_rate": 0.05, "min_data_in_leaf": 100, "max_bin": 127,
                           "num_threads": 4, "force_col_wise": True, "verbosity": -1,
                           "seed": 0, "deterministic": True, "max_cat_threshold": 32},
                          train, num_boost_round=args.rounds, valid_sets=[valid],
                          callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(100)])
        path = args.out / f"lightgbm_q{int(alpha*100)}.txt"
        model.save_model(str(path))
        metadata.append({"alpha": alpha, "best_iteration": model.best_iteration,
                         "text_bytes": path.stat().st_size})
        for start in range(0, len(eval_data), BLOCK):
            stop = min(len(eval_data), start+BLOCK)
            block = np.column_stack((eval_data.cats[start:stop], eval_data.nums[start:stop])).astype(np.float32)
            all_predictions[start:stop, j] = model.predict(block, num_threads=4)
    crossing = float(np.mean(np.any(np.diff(all_predictions, axis=1) < 0, axis=1)))
    all_predictions.sort(axis=1)
    np.save(args.out / "lightgbm_quantiles.npy", all_predictions)
    base = stored_points(args, WINNER, eval_data)
    scores = report(eval_data, {"mlp_median": base, "lightgbm_sampled": all_predictions[:, 1]})
    write(args.out / "trees.json", {"sample_rows": len(y), "full_training_rows": len(data), "models": metadata,
                                   "crossing_before_sort": crossing, "scores": scores,
                                   "deployment": "teacher candidate; browser portability not established"})


class PreparedSlice:
    """A chronological prefix for bounded random calibration sampling."""
    def __init__(self, data, n):
        self.data, self.n, self.meta = data, n, data.meta
        self.cats, self.nums, self.y = data.cats, data.nums, data.y
    def __len__(self):
        return self.n


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("screen", "trees"))
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--trials", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--sample-rows", type=int, default=1000000)
    parser.add_argument("--rounds", type=int, default=400)
    args = parser.parse_args()
    if not 0 < args.sample_rows <= 2000000 or not 0 < args.rounds <= 1000:
        parser.error("probe limits: 1..2M sample rows and 1..1000 rounds")
    cg = guard()
    if (args.out / f"{args.command}.json").exists():
        parser.error("this probe already completed; use a fresh --out directory")
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    {"screen": screen, "trees": trees}[args.command](args)
    write(args.out / f"{args.command}_resources.json", {
        "seconds": time.monotonic()-started, "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        "cgroup_peak_bytes": int((cg / "memory.peak").read_text()), "memory_max": (cg / "memory.max").read_text().strip(),
        "swap_max": (cg / "memory.swap.max").read_text().strip()})


if __name__ == "__main__":
    main()
