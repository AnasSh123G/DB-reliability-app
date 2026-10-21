#!/usr/bin/env python3
"""Measure a scratch backfill without changing its workload or resume location.

Launch multi-day runs in the capped systemd unit documented in the change design.
Use --label LABEL --memory-peak BYTES afterwards to annotate a saved report.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import math
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import sys
import tempfile
import uuid

WORK_DIR = Path(__file__).resolve().parents[1] / "data/scratch/backfill-speed"
SECONDS = ("plan_s", "sweeps_s", "finalize_s", "total_s")
COUNTERS = ("upsert_calls", "refs_refreshed")
EXTRA_OPTIONS = {
    "--bulk-pragmas", "--no-bulk-pragmas", "--merge-plan-hours",
    "--no-merge-plan-hours", "--plan-writer",
}


def _same_file(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))


def _dotenv_value(directory: Path, name: str) -> str | None:
    """Read only a path setting; never import or print application secrets."""
    dotenv = directory / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            key, separator, candidate = line.strip().partition("=")
            if separator and key.strip() == name:
                return candidate.strip().strip("\"'")
    return None


def _configured_data(directory: Path) -> Path:
    value = os.environ.get("DBREL_DATA_DIR")
    if value is None:
        value = _dotenv_value(directory, "DBREL_DATA_DIR")
    path = Path("data" if value is None else value)
    return path if path.is_absolute() else directory / path


def _defaults(checkout: Path, scratch_override: Path | None = None) -> set[Path]:
    bases = {Path.cwd(), checkout, Path(__file__).resolve().parents[1]}
    defaults = {base / "data/dbrel.sqlite" for base in bases} | {
        _configured_data(base) / "dbrel.sqlite" for base in bases}
    for base in bases:
        # Persistent live identities stay protected even during a scratch override.
        custom = _dotenv_value(base, "DBREL_DB")
        if custom is not None:
            defaults.add(base / Path(custom))
        data = _dotenv_value(base, "DBREL_DATA_DIR")
        if data is not None:
            defaults.add(base / Path(data) / "dbrel.sqlite")
    inherited = os.environ.get("DBREL_DB")
    if inherited is not None:
        inherited_path = checkout / Path(inherited)
        if scratch_override is None or inherited_path.resolve() != scratch_override:
            defaults.add(inherited_path)
    return defaults


def _protected_files(databases: set[Path]) -> set[Path]:
    protected = set()
    for source in databases:
        for spelling in (source, source.resolve()):
            protected.update(Path(str(spelling) + suffix)
                             for suffix in ("", "-wal", "-shm", ".writer.lock"))
            protected.add(spelling.parent / "tt_sweep.lock")
    return protected


def _save(path: Path, report: dict) -> None:
    """Publish a complete JSON document, including failed-run reports."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        json.dump(report, temp, indent=2, allow_nan=False)
        temp.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _extra_args(value: str) -> list[str]:
    tokens = shlex.split(value)
    index = 0
    while index < len(tokens):
        option, _, attached = tokens[index].partition("=")
        if option not in EXTRA_OPTIONS:
            raise ValueError(f"extra argument cannot change the benchmark workload: {option}")
        if option == "--plan-writer":
            if not attached:
                index += 1
                attached = tokens[index] if index < len(tokens) else ""
            if attached not in {"per_run", "batch"}:
                raise ValueError("--plan-writer requires per_run or batch")
        elif attached:
            raise ValueError(f"boolean option does not take a value: {option}")
        index += 1
    return tokens


def _timing(line: str) -> dict:
    words = line.split()
    day = date.fromisoformat(words[1]).isoformat()
    pairs = [word.split("=", 1) for word in words[2:]]
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate timing field")
    row = {"date": day}
    for name in SECONDS:
        value = float(fields[name])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid timing value: {name}")
        row[name] = value
    for name in COUNTERS:
        value = int(fields[name])
        if value < 0:
            raise ValueError(f"invalid counter: {name}")
        row[name] = value
    if row["total_s"] < sum(row[name] for name in SECONDS[:3]) - 0.01:
        raise ValueError("phase timings exceed total time")
    return row


def _paths(args, work: Path) -> tuple[Path, Path, Path, Path, Path]:
    checkout, database = args.checkout.resolve(), args.db.resolve()
    defaults = _defaults(checkout, database if database.is_relative_to(work) else None)
    if not checkout.is_dir() or not database.is_file():
        raise ValueError("checkout directory and existing scratch database are required")
    if any(_same_file(database, live) for live in defaults):
        raise ValueError("refusing a live database; use a fresh scratch copy")
    report = work / "bench" / f"{args.label}.json"
    log = work / "logs" / f"{args.label}.log"
    resume = work / f"{args.label}.replayed.json"
    profile = work / "bench" / f"{args.label}.pyspy.txt"
    protected = _protected_files(defaults | {args.db, database})
    outputs = (report, log, resume, profile, work / "bench" / f"{args.label}.replay-status.json")
    for output in outputs:
        if any(_same_file(output, source) for source in protected):
            raise ValueError(f"benchmark output aliases a protected database file: {output}")
    for index, output in enumerate(outputs):
        if any(_same_file(output, other) for other in outputs[index + 1:]):
            raise ValueError("benchmark output paths must be distinct")
    return checkout, database, report, log, resume


def _stream(command, checkout, environment, log_path, start, end):
    days, seen, valid = [], set(), True
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log, subprocess.Popen(
        command, cwd=checkout, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    ) as child:
        for chunk in child.stdout:
            log.write(chunk)
            log.flush()
            print(chunk, end="", flush=True)
            for line in chunk.splitlines():
                if not line.startswith("timings "):
                    continue
                try:
                    row = _timing(line)
                    if row["date"] in seen or not start.isoformat() <= row["date"] <= end.isoformat():
                        raise ValueError("duplicate or out-of-window timing day")
                    seen.add(row["date"])
                    days.append(row)
                except (ValueError, KeyError, IndexError) as exc:
                    valid = False
                    print(f"invalid timings: {exc}", file=sys.stderr)
        return days, valid, child.wait()


def _profile_status(status_path, run_token, sampler_returncode):
    code = None
    try:
        status = json.loads(status_path.read_text())
        if (isinstance(status, dict) and status.get("run_token") == run_token
                and type(status.get("returncode")) is int):
            code = status["returncode"]
        else:
            print("profile completion evidence is stale or malformed", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"profile completion evidence unavailable: {exc}", file=sys.stderr)
    return {"sampler_returncode": sampler_returncode, "returncode": code}


def _completed(resume, expected):
    try:
        done = json.loads(resume.read_text())
        if (isinstance(done, list) and len(done) == len(expected)
                and all(type(day) is str and day in expected for day in done)
                and len(set(done)) == len(expected)):
            return True
    except (OSError, ValueError) as exc:
        print(f"fresh replay completion evidence unavailable: {exc}", file=sys.stderr)
    print("not every requested day has a fresh completion record", file=sys.stderr)
    return False


def _command(args, work, checkout, backfill, status_path, run_token):
    if not args.profile:
        return ["uv", "run", "--directory", str(checkout), "dbrel", *backfill]
    status_path.unlink(missing_ok=True)
    return ["uvx", "py-spy", "record", "--idle", "--rate", "50", "--format", "raw",
            "-o", str(work / "bench" / f"{args.label}.pyspy.txt"), "--",
            str(checkout / ".venv/bin/python"), str(Path(__file__).resolve()),
            "--profile-child", str(status_path), run_token,
            str(checkout / ".venv/bin/dbrel"), *backfill]


def _run(args, work: Path) -> int:
    if not all((args.checkout, args.db, args.date_from, args.date_to)):
        raise ValueError("--checkout, --db, --from and --to are required for replay")
    start, end = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    if end < start:
        raise ValueError("--to must not precede --from")
    extra = _extra_args(args.extra_args)
    checkout, database, report_path, log_path, resume = _paths(args, work)
    resume.unlink(missing_ok=True)
    root = (args.root if args.root is not None else work / "archive").resolve()
    backfill = ["backfill", "--from", start.isoformat(), "--to", end.isoformat(),
                "--restart", "--timings", "--root", str(root), "--done-file", str(resume), *extra]
    status_path = work / "bench" / f"{args.label}.replay-status.json"
    run_token = uuid.uuid4().hex
    command = _command(args, work, checkout, backfill, status_path, run_token)
    report = dict(label=args.label, checkout=str(checkout), db=str(database), root=str(root),
                  date_from=start.isoformat(), date_to=end.isoformat(), command=command,
                  days=[], complete=False, returncode=None, memory_peak_bytes=args.memory_peak)
    _save(report_path, report)
    environment = {**os.environ, "DBREL_DB": str(database), "PYTHONUNBUFFERED": "1"}
    print(f"W={work}", flush=True)
    print(f"DBREL_DB={database}", flush=True)
    days, valid, returncode = _stream(command, checkout, environment, log_path, start, end)
    outcome = (_profile_status(status_path, run_token, returncode) if args.profile
               else {"returncode": returncode})
    expected = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
    complete = (valid and {day["date"] for day in days} == expected
                and _completed(resume, expected) and outcome["returncode"] == 0
                and outcome.get("sampler_returncode", 0) == 0)
    _save(report_path, {**report, **outcome, "days": days, "complete": complete})
    return 0 if complete else 1


def _profile_child(arguments: list[str]) -> int:
    """Run the console script in the sampled process and attest its own exit."""
    if len(arguments) < 3:
        raise ValueError("profile child needs status path, token and console script")
    status_path, token, executable, *console_args = arguments
    executable = Path(executable).resolve()
    databases = _defaults(Path.cwd())
    if os.environ.get("DBREL_DB"):
        databases.add(Path(os.environ["DBREL_DB"]))
    protected = _protected_files(databases) | {executable}
    if any(_same_file(Path(status_path), path) for path in protected):
        raise ValueError("profile status must not replace a database or console script")
    sys.argv = [str(executable), *console_args]
    sys.path[0] = str(executable.parent)
    code = 1
    try:
        runpy.run_path(str(executable), run_name="__main__")
        code = 0
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        raise
    except KeyboardInterrupt:
        code = 130
        raise
    finally:
        _save(Path(status_path), {"run_token": token, "returncode": code})
    return code


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "--profile-child":
        return _profile_child(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--from", dest="date_from")
    parser.add_argument("--to", dest="date_to")
    parser.add_argument("--label", required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--extra-args", default="")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--memory-peak", type=int)
    try:
        args = parser.parse_args(arguments)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.label):
            raise ValueError("label must be a filename-safe identifier")
        if args.memory_peak is not None and args.memory_peak < 0:
            raise ValueError("memory peak must be non-negative")
        work = WORK_DIR.resolve()
        if args.memory_peak is not None and not any((args.checkout, args.db, args.date_from, args.date_to)):
            path = work / "bench" / f"{args.label}.json"
            report = json.loads(path.read_text())
            if not isinstance(report, dict) or report.get("label") != args.label:
                raise ValueError("invalid existing benchmark report")
            _save(path, {**report, "memory_peak_bytes": args.memory_peak})
            return 0
        return _run(args, work)
    except SystemExit as exc:
        return int(exc.code)
    except (ValueError, OSError) as exc:
        print(f"benchmark error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
