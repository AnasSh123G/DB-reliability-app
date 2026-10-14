"""Run the frozen compact-model experiment serially inside one capped service."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "data/ml/compact_training_20261013"
PYTHON = ROOT / "data/build/torch-rocm/venv/bin/python"
GIB = 2**30


def available():
    return int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:")))*1024


def main():
    cg = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    directory = Path("/sys/fs/cgroup") / cg.lstrip("/")
    cap = (directory / "memory.max").read_text().strip()
    if cap == "max" or int(cap) > 8*GIB or (directory / "memory.swap.max").read_text().strip() != "0":
        raise SystemExit("a private <=8G cgroup with zero swap is required")
    os.chdir(ROOT)
    (WORK / "tmp").mkdir(parents=True, exist_ok=True)
    logs = WORK / "logs"
    logs.mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(WORK / "tmp")
    os.environ["OMP_NUM_THREADS"] = os.environ["OPENBLAS_NUM_THREADS"] = "4"
    # Re-run the small shape probes under the final resume fingerprint, then
    # each full fit in a fresh child so GPU/native allocation cannot accumulate.
    jobs = [("probe", track, rows) for track in ("wide", "distribution") for rows in (100000, 500000)]
    jobs += [("train", track, None) for track in ("fine", "wide", "distribution")]
    state = {}
    for command, track, rows in jobs:
        if available() < int(cap)+4*GIB:
            raise SystemExit("insufficient free memory for the hard cap plus four GiB desktop headroom")
        name = f"{command}-{track}" + ("" if rows is None else f"-{rows}")
        argv = [str(PYTHON), "-u", "scripts/train_compact_delay.py", command, "--track", track]
        if rows is not None:
            argv += ["--rows", str(rows)]
        path = logs / f"{name}-{time.time_ns()}.log"
        print(f"START {name}; log={path.relative_to(ROOT)}", flush=True)
        with path.open("w") as fh:
            child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in child.stdout:
                fh.write(line); fh.flush(); print(line, end="", flush=True)
            code = child.wait()
        state[name] = {"exit_code": code, "log": str(path.relative_to(ROOT))}
        (WORK / "queue.json").write_text(json.dumps(state, indent=2))
        if code != 0:
            raise SystemExit(f"{name} failed; checkpoint retained; later jobs not started")
        if command == "probe":
            candidates = list((WORK / "probes").glob(f"{track}-{rows}-*/result.json"))
            result = json.loads(max(candidates, key=lambda p: p.stat().st_mtime_ns).read_text())
            if result["resources"]["cgroup_peak_bytes"]*1.15 > int(cap):
                raise SystemExit("memory probe does not leave 15% headroom; full fits refused")
    print("All compact training jobs completed", flush=True)


if __name__ == "__main__":
    main()
