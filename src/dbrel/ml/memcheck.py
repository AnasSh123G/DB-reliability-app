"""Measure each library's real memory peak before a multi-hour run, and refuse runs that won't fit.

For every library, short probe fits (at most `PROBE_ROUNDS` rounds) run at:
- the tuning sample size, on the departure side
- two smaller slices of the final-fit training set

Each probe runs in its own child process, so it measures only its own fit. Its peak is the
kernel's high-water mark, reset just before the fit starts. Under the launch unit's no-swap
cap, resident memory is the whole footprint. The probes use the most memory-hungry corner of
each library's approved search space. When a library's tuning has already finished, its tuned
parameters are probed as well, since those are what its final fits will use.

The final-fit peak is projected linearly in rows to each side's full size. Model growth per
round, measured between two points of the probe, is added for `max_rounds`. The result goes
to `<dataset-dir>/memory_check.json`. `gate()` reads that file and refuses to launch a
bake-off whose projected peak, plus `MARGIN`, is above the cap.

Run it capped with `--oom-policy continue`: a probe that hits the cap is killed on its own,
recorded as over the cap, and the check carries on with the next probe.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

PROBE_SIZES = (1_000_000, 2_000_000)
PROBE_ROUNDS = 100
MARGIN = 1.15
RESULT_NAME = "memory_check.json"

# The corner of each approved search space (tune.py) that needs the most memory.
CORNER = {
    "lightgbm": {"num_leaves": 1023, "learning_rate": 0.1, "min_data_in_leaf": 20,
                 "feature_fraction": 1.0, "bagging_fraction": 1.0, "lambda_l2": 0.0,
                 "cat_smooth": 10.0, "max_cat_threshold": 256},
    "catboost": {"depth": 10, "learning_rate": 0.1, "l2_leaf_reg": 3.0, "rsm": 1.0,
                 "bootstrap_type": "Bernoulli", "subsample": 1.0, "one_hot_max_size": 2,
                 "border_count": 254},
    "xgboost": {"max_depth": 12, "max_leaves": 1023, "eta": 0.1, "min_child_weight": 1.0,
                "colsample_bytree": 1.0, "subsample": 1.0, "lambda": 1.0,
                "max_cat_to_onehot": 1, "max_bin": 512},
}
from dbrel.ml.launch import parse_size  # noqa: E402 - one parser for systemd sizes


# ---------------------------------------------------------------------------
# child: one probe fit
# ---------------------------------------------------------------------------


def _vm(field: str) -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(field + ":"):
            return int(line.split()[1]) * 1024
    return 0


def _probe(spec: dict) -> dict:
    import numpy as np

    from dbrel.ml.bakeoff import _matrix, _prepare, _tuning_rows
    from dbrel.ml.dataset import load_dataset
    from dbrel.ml.memory import release_memory
    from dbrel.ml.tune import make_candidate

    ds = load_dataset(Path(spec["dataset_dir"]))
    _, _, features, sides = _prepare(ds)
    ds.features = None
    full_rows = {side: int(len(s.train)) for side, s in sides.items()}
    side = sides[spec["side"]]
    if spec["stage"] == "tuning":
        rows = _tuning_rows(sides["dep"].train, spec["rows"])
    else:
        n = min(spec["rows"], len(side.train))
        rng = np.random.default_rng(1)
        rows = side.train[np.sort(rng.choice(len(side.train), size=n, replace=False))]
    X, y = _matrix(features, rows), side.y[rows]
    X_val, y_val = _matrix(features, side.validation), side.y[side.validation]
    del features, sides
    release_memory()

    Path("/proc/self/clear_refs").write_text("5")  # reset VmHWM: the peak from here on is the fit's
    base = _vm("VmRSS")
    at_step: dict[int, int] = {}

    def reporter(step: int, value: float) -> bool:
        at_step.setdefault(step, _vm("VmRSS"))
        return False

    params = dict(spec["params"])
    # LightGBM tunes on its median booster alone but fits all three for real (tune.py).
    kwargs = {"quantiles": (0.5,)} if spec["library"] == "lightgbm" and spec["stage"] == "tuning" else {}
    candidate = make_candidate(spec["library"], params, threads=spec["threads"],
                               max_rounds=spec["rounds"], **kwargs)
    started = time.monotonic()
    candidate.fit(X, y, X_val, y_val, reporter=reporter)
    seconds = time.monotonic() - started
    rounds = max(candidate.best_rounds) if candidate.best_rounds else spec["rounds"]
    steps = sorted(at_step)
    growth = 0.0
    if len(steps) >= 2:
        a, b = steps[len(steps) // 2 - 1], steps[-1]
        growth = max(0.0, (at_step[b] - at_step[a]) / (b - a))
    return {
        "rows": int(len(rows)), "validation_rows": int(len(y_val)), "base_rss": base,
        "peak": _vm("VmHWM"), "swap": _vm("VmSwap"), "seconds": round(seconds, 1),
        "rounds": int(rounds), "seconds_per_round": seconds / max(rounds, 1),
        "growth_per_round": growth, "full_train_rows": full_rows,
    }


# ---------------------------------------------------------------------------
# parent: run the probes, project, write the result
# ---------------------------------------------------------------------------


def _run_child(spec: dict, cap: int | None) -> dict:
    result = subprocess.run([sys.executable, "-m", "dbrel.ml.memcheck", json.dumps(spec)],
                            capture_output=True, text=True)
    if result.returncode == -9:
        return {"killed": True, "rows": spec["rows"], "peak": cap,
                "note": "killed at the memory cap (or by SIGKILL)"}
    if result.returncode != 0:
        raise RuntimeError(f"probe {spec['library']} {spec['stage']} {spec['rows']:,} rows failed:\n"
                           + result.stderr[-3000:])
    return {"killed": False, **json.loads(result.stdout.strip().splitlines()[-1])}


def _own_cap() -> int | None:
    from dbrel.ml.memory import CGROUP_ROOT, launched_unit, own_cgroup

    cgroup = own_cgroup()
    if launched_unit(cgroup) is None:
        return None
    text = (CGROUP_ROOT / cgroup.lstrip("/") / "memory.max").read_text().strip()
    return None if text == "max" else int(text)


def project(final_probes: list[dict], full_rows: int, max_rounds: int) -> int | None:
    """Peak at `full_rows`, linear in rows from the probes, plus model growth to `max_rounds`."""
    if any(p["killed"] for p in final_probes):
        return None  # at least one probe alone did not fit: the full fit cannot either
    (n1, p1), (n2, p2) = sorted((p["rows"], p["peak"]) for p in final_probes)[:2]
    slope = max(0.0, (p2 - p1) / (n2 - n1)) if n2 > n1 else 0.0
    growth = max(p["growth_per_round"] for p in final_probes)
    rounds = max(p["rounds"] for p in final_probes)
    return int(p2 + slope * (full_rows - n2) + growth * max(0, max_rounds - rounds))


def run_memory_check(dataset_dir: Path, libraries: tuple[str, ...], *, threads: int, log=print,
                     sizes: tuple[int, int] = PROBE_SIZES, tuning_rows: int | None = None,
                     rounds: int = PROBE_ROUNDS, max_rounds: int = 5000) -> dict:
    from dbrel.ml.tune import TRIAL_SAMPLE_ROWS

    tuning_rows = tuning_rows or TRIAL_SAMPLE_ROWS
    meta = json.loads((dataset_dir / "dataset.json").read_text())
    cap = _own_cap()
    out = {"checked_at": time.time(), "dataset_built_at": meta["built_at"], "threads": threads,
           "unit_cap": cap, "margin": MARGIN, "max_rounds": max_rounds, "probe_rounds": rounds,
           "libraries": {}}
    for library in libraries:
        kinds = {"corner": CORNER[library]}
        tuning_file = dataset_dir / "runs" / library / "tuning.json"
        if tuning_file.exists():
            kinds["tuned"] = json.loads(tuning_file.read_text())["tuning"]["best_params"]
        entry = {"kinds": {}}
        for kind, params in kinds.items():
            base = {"library": library, "params": params, "threads": threads, "rounds": rounds,
                    "dataset_dir": str(dataset_dir)}
            probes = {}
            log(f"{library} [{kind}]: tuning-size probe, {tuning_rows:,} rows ...")
            probes["tuning"] = _run_child({**base, "stage": "tuning", "side": "dep",
                                           "rows": tuning_rows}, cap)
            full = probes["tuning"].get("full_train_rows")
            for side in ("dep", "arr"):
                for n in sizes:
                    log(f"{library} [{kind}]: final-fit probe, {side} side, {n:,} rows ...")
                    probes[f"{side}_{n}"] = _run_child({**base, "stage": "final", "side": side,
                                                        "rows": n}, cap)
                    full = full or probes[f"{side}_{n}"].get("full_train_rows")
            projected = {}
            for side in ("dep", "arr"):
                finals = [probes[f"{side}_{n}"] for n in sizes]
                projected[side] = None if full is None else project(finals, full[side], max_rounds)
            tuning = probes["tuning"]
            tuning_peak = None if tuning["killed"] else int(
                tuning["peak"] + tuning["growth_per_round"] * max(0, max_rounds - tuning["rounds"]))
            per_round = {side: [probes[f"{side}_{n}"].get("seconds_per_round") for n in sizes]
                         for side in ("dep", "arr")}
            entry["kinds"][kind] = {"params": params, "probes": probes, "tuning_peak": tuning_peak,
                                    "projected_final_peak": projected, "full_train_rows": full,
                                    "seconds_per_round": {"tuning": tuning.get("seconds_per_round"),
                                                          **per_round}}
            log(f"{library} [{kind}]: tuning peak {_gib(tuning_peak)}, projected final-fit peak "
                f"dep {_gib(projected['dep'])}, arr {_gib(projected['arr'])}")
        out["libraries"][library] = entry
    (dataset_dir / RESULT_NAME).write_text(json.dumps(out, indent=1))
    return out


def _gib(value) -> str:
    return "over the cap" if value is None else f"{value / 2**30:.1f} GiB"


# ---------------------------------------------------------------------------
# the launch gate
# ---------------------------------------------------------------------------


def _bakeoff_libraries(step: list[str]) -> tuple[list[str], Path | None] | None:
    """(libraries, dataset dir) if `step` is a `dbrel ml bakeoff` command, else None."""
    if "bakeoff" not in step or "ml" not in step or step.index("ml") + 1 != step.index("bakeoff"):
        return None
    libraries, dataset_dir = "lightgbm,catboost,xgboost", None
    for i, token in enumerate(step):
        if token == "--libraries" and i + 1 < len(step):
            libraries = step[i + 1]
        elif token.startswith("--libraries="):
            libraries = token.split("=", 1)[1]
        elif token == "--dataset-dir" and i + 1 < len(step):
            dataset_dir = Path(step[i + 1])
        elif token.startswith("--dataset-dir="):
            dataset_dir = Path(token.split("=", 1)[1])
    return [lib.strip() for lib in libraries.split(",") if lib.strip()], dataset_dir


def needed_bytes(entry: dict, *, tuning_done: bool) -> tuple[int | None, str]:
    """The largest projected peak a library still has ahead of it, and what it is based on."""
    kinds = entry["kinds"]
    final_kind = "tuned" if tuning_done and "tuned" in kinds else "corner"
    peaks = list(kinds[final_kind]["projected_final_peak"].values())
    if not tuning_done:
        peaks.append(kinds["corner"]["tuning_peak"])
    if any(p is None for p in peaks):
        return None, final_kind
    return max(peaks), final_kind


def gate(steps: list[list[str]], memory: str, default_dataset_dir: Path) -> None:
    """Raise LaunchError if a bake-off step would need more memory than `memory` allows."""
    from dbrel.ml.launch import LaunchError

    cap = parse_size(memory)
    for step in steps:
        parsed = _bakeoff_libraries(step)
        if parsed is None:
            continue
        libraries, dataset_dir = parsed
        dataset_dir = dataset_dir or default_dataset_dir
        runs = dataset_dir / "runs"
        pending = [lib for lib in libraries if not (runs / lib / "done").exists()]
        if not pending:
            continue
        path = dataset_dir / RESULT_NAME
        if not path.exists():
            raise LaunchError(f"no memory check for this bake-off -- run `dbrel ml launch memcheck "
                              f"--oom-policy continue -- dbrel ml memory-check --libraries "
                              f"{','.join(pending)}` first, or pass --ignore-memory-check")
        check = json.loads(path.read_text())
        meta = json.loads((dataset_dir / "dataset.json").read_text())
        if check["dataset_built_at"] != meta["built_at"]:
            raise LaunchError("the memory check was made on a different dataset -- rerun it")
        problems = []
        for library in pending:
            entry = check["libraries"].get(library)
            if entry is None:
                problems.append(f"{library}: not in the memory check")
                continue
            need, basis = needed_bytes(entry, tuning_done=(runs / library / "tuning.json").exists())
            if need is None:
                problems.append(f"{library}: a probe hit the cap on its own ({basis} parameters)")
            elif need * MARGIN > cap:
                problems.append(f"{library}: projected peak {need / 2**30:.1f} GiB "
                                f"(+{MARGIN - 1:.0%} margin = {need * MARGIN / 2**30:.1f} GiB, "
                                f"{basis} parameters) is above the cap {memory}")
        if problems:
            raise LaunchError("memory check says this bake-off would not fit -- "
                              + "; ".join(problems) + ". (--ignore-memory-check overrides)")


if __name__ == "__main__":
    print(json.dumps(_probe(json.loads(sys.argv[1]))), flush=True)
