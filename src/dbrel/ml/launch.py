"""Start training jobs as capped systemd user services, and read back how they ended.

A job started from an editor's terminal inherits the editor's cgroup, and
`setsid`/`nohup` do not change that. A transient service under the user
manager's `background.slice` belongs to no terminal, editor or Claude session,
so closing any of them leaves it running.

- `MemoryMax` + `MemorySwapMax=0`: a hard cap with no swap (swap here is zram,
  i.e. RAM). Reaching it kills the job alone, quickly, instead of thrashing
  zram until the kernel OOM-kills whatever it picks.
- `RemainAfterExit=yes`, no `--collect`: the unit, and with it `Result=` and
  the exit status, stays inspectable after it ends.
- Output is appended to a new timestamped log per launch; nothing is overwritten.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dbrel.ml.memory import UNIT_PREFIX

CHAIN_SEPARATOR = "::"
DEFAULT_MEMORY = "18G"
HEADROOM = 2**30  # kept free for the desktop on top of the cap
_SIZE_UNITS = {"K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_MEMORY = re.compile(r"^\d+(\.\d+)?[KMGT]$")
_SHOW = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus", "ExecMainCode",
         "ExecMainStartTimestamp", "ExecMainExitTimestamp", "MemoryPeak", "MemoryMax",
         "MemorySwapMax", "ControlGroup", "InvocationID")


class LaunchError(RuntimeError):
    pass


def parse_size(text: str) -> int:
    """Bytes for a systemd size like 18G or 300M (1024-based, as systemd reads it)."""
    return int(float(text[:-1]) * _SIZE_UNITS[text[-1]])


def available_memory() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise LaunchError("cannot read MemAvailable from /proc/meminfo")


def check_cap_fits(memory: str, available: int) -> None:
    """A cap only protects the desktop if the memory under it is really free.

    With a cap above what is available, the machine runs out before the cap is
    reached, and the kernel then kills whatever it picks, anywhere.
    """
    cap = parse_size(memory)
    if cap + HEADROOM > available:
        usable = max(0, (available - HEADROOM) // 2**30)
        raise LaunchError(
            f"MemoryMax={memory} needs {(cap + HEADROOM) / 2**30:.1f} GiB free (cap + 1 GiB for the "
            f"desktop), but only {available / 2**30:.1f} GiB is available -- use --memory {usable}G "
            "or less, or close something first")


def unit_for(name: str) -> str:
    if not _NAME.match(name):
        raise LaunchError(f"job name {name!r}: use lowercase letters, digits and dashes")
    return f"{UNIT_PREFIX}{name}.service"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def unit_state(unit: str) -> dict[str, str]:
    out = _systemctl("show", unit, "--property=" + ",".join(_SHOW)).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def is_running(state: dict[str, str]) -> bool:
    """Active with a live process; `active (exited)` is a finished RemainAfterExit unit."""
    if state.get("ActiveState") in ("activating", "deactivating", "reloading"):
        return True
    return state.get("ActiveState") == "active" and state.get("SubState") != "exited"


def split_chain(command: list[str]) -> list[list[str]]:
    steps, current = [], []
    for token in command:
        if token == CHAIN_SEPARATOR:
            steps.append(current)
            current = []
        else:
            current.append(token)
    steps.append(current)
    if any(not step for step in steps):
        raise LaunchError(f"empty command around {CHAIN_SEPARATOR!r}")
    return steps


def resolve_executable(argv0: str) -> str:
    """Absolute path for argv[0]; the project's venv wins, as it does in this shell."""
    if "/" in argv0:
        # absolute(), not resolve(): a venv's python is a symlink, and following it would run
        # the base interpreter without the venv's packages.
        path = Path(argv0).absolute()
        if not path.exists():
            raise LaunchError(f"no such program: {argv0}")
        return str(path)
    venv_bin = Path(sys.executable).parent
    found = shutil.which(argv0, path=f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    if found is None:
        raise LaunchError(f"program not found: {argv0}")
    return found


def _environment() -> dict[str, str]:
    """What the job needs from this shell: DBREL_* and GPU (ROCm/HIP) settings, the venv on PATH."""
    env = {k: v for k, v in os.environ.items() if k.startswith(("DBREL_", "ROCM_", "HIP_", "HSA_"))}
    venv_bin = str(Path(sys.executable).parent)
    env["PATH"] = f"{venv_bin}{os.pathsep}/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin"
    env["PYTHONUNBUFFERED"] = "1"
    return env


_STAMPED_LOG = re.compile(r"^\d{8}T\d{6}Z\.log$")


def latest_log(logs: Path, name: str) -> Path | None:
    """This job's newest log: `<name>-<UTC stamp>.log`, never another job's."""
    mine = sorted(p for p in logs.glob(f"{name}-*.log") if _STAMPED_LOG.match(p.name[len(name) + 1:]))
    return mine[-1] if mine else None


def build_command(unit: str, steps: list[list[str]], *, memory: str, oom_policy: str,
                  workdir: Path, log: Path) -> list[str]:
    steps = [[resolve_executable(step[0]), *step[1:]] for step in steps]
    if len(steps) == 1:
        argv = steps[0]
    else:
        argv = [sys.executable, "-m", "dbrel.ml.chain", json.dumps(steps)]
    cmd = [
        "systemd-run", "--user", f"--unit={unit}", "--slice=background.slice",
        f"--description=dbrel ml job ({log.name})",
        "-p", f"MemoryMax={memory}", "-p", "MemorySwapMax=0", "-p", f"OOMPolicy={oom_policy}",
        "-p", "Nice=10", "-p", "RemainAfterExit=yes", "-p", f"WorkingDirectory={workdir}",
        "-p", f"StandardOutput=append:{log}", "-p", f"StandardError=append:{log}",
    ]
    for key, value in sorted(_environment().items()):
        cmd.append(f"--setenv={key}={value}")
    return [*cmd, "--", *argv]


def launch(name: str, command: list[str], *, workdir: Path, logs: Path,
           memory: str = DEFAULT_MEMORY, oom_policy: str = "stop", gate=None,
           note: str | None = None) -> tuple[str, Path]:
    """Start `command` (steps joined by `::`) as a capped unit. Returns (unit, log path).

    `gate(steps, memory)` may raise LaunchError to refuse (the memory check).
    """
    if not _MEMORY.match(memory):
        raise LaunchError(f"--memory {memory!r}: use a size like 18G or 12000M")
    if oom_policy not in ("stop", "continue", "kill"):
        raise LaunchError(f"unknown OOM policy {oom_policy!r}")
    unit = unit_for(name)
    steps = split_chain(command)
    check_cap_fits(memory, available_memory())
    if gate is not None:
        gate(steps, memory)

    state = unit_state(unit)
    if is_running(state):
        raise LaunchError(f"{unit} is still running -- `dbrel ml status {name}`, "
                          f"or stop it with `systemctl --user stop {unit}`")
    if state.get("LoadState") == "loaded" and state.get("ActiveState") == "active":
        _systemctl("stop", unit)  # a finished RemainAfterExit unit
    _systemctl("reset-failed", unit)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = logs / f"{name}-{stamp}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        raise LaunchError(f"{log} already exists")
    cmd = build_command(unit, steps, memory=memory, oom_policy=oom_policy, workdir=workdir, log=log)
    with log.open("x") as fh:
        fh.write(f"# launched {stamp} as {unit}, MemoryMax={memory}, MemorySwapMax=0, "
                 f"OOMPolicy={oom_policy}\n")
        for i, step in enumerate(steps, 1):
            fh.write(f"# step {i}: {' '.join(step)}\n")
        if note:
            fh.write(f"# {note}\n")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise LaunchError(f"systemd-run failed: {result.stderr.strip() or result.stdout.strip()}")
    return unit, log


def describe(name: str, logs: Path) -> dict:
    """How the job stands or ended: unit state, exit reason, memory peak, log, journal tail."""
    unit = unit_for(name)
    state = unit_state(unit)
    loaded = state.get("LoadState") == "loaded"
    journal = subprocess.run(
        ["journalctl", "--user", "-u", unit, "-n", "12", "-o", "short-iso", "--no-pager"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not loaded:
        verdict = "unknown (unit not loaded; see journal)"
    elif is_running(state):
        verdict = "running"
    elif state.get("Result") == "oom-kill":
        verdict = "killed: memory limit reached"
    elif state.get("Result") == "success" and state.get("ExecMainStatus") == "0":
        verdict = "finished successfully"
    elif state.get("ActiveState") == "inactive" and not state.get("ExecMainStartTimestamp"):
        verdict = "never started"
    else:
        how = {"1": "exited", "2": "killed by a signal", "3": "dumped core"}.get(
            state.get("ExecMainCode", ""), f"code {state.get('ExecMainCode')}")
        verdict = f"failed: result={state.get('Result')}, {how}, status {state.get('ExecMainStatus')}"
    return {"unit": unit, "verdict": verdict, "state": state, "log": latest_log(logs, name),
            "journal": journal}
