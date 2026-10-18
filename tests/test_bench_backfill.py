"""Unit tests for the isolated backfill benchmark wrapper.

Every child process is faked; these tests never replay an archive or use a live
database. The benchmark workspace is relative to a temporary working directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import date, timedelta
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_backfill.py"
REAL_POPEN = subprocess.Popen


def _load_bench():
    spec = importlib.util.spec_from_file_location("bench_backfill_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load benchmark script at {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeProcess:
    def __init__(self, argv, *, lines=(), returncode=0, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.stdout = iter(lines)
        self.returncode = returncode
        self.waited = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def wait(self):
        self.waited = True
        return self.returncode


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    db = tmp_path / "scratch.sqlite"
    db.write_bytes(b"scratch database fixture")
    calls = []
    fake = {
        "lines": [], "returncode": 0, "replay_returncode": 0,
        "marker": "matching", "run_token": None, "marker_preexisting": None,
        "done_mode": "complete", "resume_preexisting": None,
    }

    def popen(argv, **kwargs):
        argv = list(argv)
        done_path = Path(argv[argv.index("--done-file") + 1])
        fake["resume_preexisting"] = done_path.exists()
        if fake["done_mode"] != "missing":
            first = date.fromisoformat(argv[argv.index("--from") + 1])
            last = date.fromisoformat(argv[argv.index("--to") + 1])
            expected = [(first + timedelta(days=index)).isoformat()
                        for index in range((last - first).days + 1)]
            records = {
                "complete": expected,
                "partial": expected[:1],
                "extra": [*expected, "2026-09-22"],
                "duplicate": [*expected, expected[-1]],
                "badtypes": [*expected[:-1], 7],
            }
            done_path.parent.mkdir(parents=True, exist_ok=True)
            if fake["done_mode"] == "malformed":
                done_path.write_text("{not json")
            else:
                done_path.write_text(json.dumps(records[fake["done_mode"]]))
        if "--profile-child" in argv:
            marker_at = argv.index("--profile-child") + 1
            marker_path, run_token = Path(argv[marker_at]), argv[marker_at + 1]
            fake["run_token"] = run_token
            fake["marker_preexisting"] = marker_path.exists()
            if fake["marker"] != "missing":
                token = "stale-token" if fake["marker"] == "stale" else run_token
                marker_path.parent.mkdir(parents=True, exist_ok=True)
                marker_path.write_text(json.dumps({
                    "run_token": token,
                    "returncode": fake["replay_returncode"],
                }))
        process = _FakeProcess(
            argv,
            lines=fake["lines"],
            returncode=fake["returncode"],
            **kwargs,
        )
        calls.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    bench = _load_bench()
    monkeypatch.setattr(bench, "WORK_DIR", tmp_path)
    return bench, tmp_path, checkout, db, fake, calls


def _args(checkout, db, *extra):
    return [
        "--checkout", str(checkout),
        "--db", str(db),
        "--from", "2026-09-20",
        "--to", "2026-09-21",
        "--label", "trial-a",
        *extra,
    ]


def _timing(day, *, plan="0.1", sweeps="0.2", finalize="0.3", total="0.6",
            upserts="12", refreshed="2"):
    return (
        f"timings {day} plan_s={plan} sweeps_s={sweeps} finalize_s={finalize} "
        f"total_s={total} upsert_calls={upserts} refs_refreshed={refreshed}\n"
    )


def test_success_records_exact_command_environment_timings_and_logs(harness):
    bench, work, checkout, db, fake, calls = harness
    fake["lines"] = ["child started\n", _timing("2026-09-20"), _timing("2026-09-21")]

    result = bench.main(_args(checkout, db, "--root", str(work / "archive")))

    assert result == 0
    assert len(calls) == 1
    process = calls[0]
    expected = [
        "uv", "run", "--directory", str(checkout), "dbrel", "backfill",
        "--from", "2026-09-20", "--to", "2026-09-21", "--restart", "--timings",
        "--root", str(work / "archive"), "--done-file", str(work / "trial-a.replayed.json"),
    ]
    assert process.argv == expected
    assert Path(process.kwargs["cwd"]) == checkout
    assert fake["resume_preexisting"] is False
    assert process.kwargs["env"]["DBREL_DB"] == str(db.resolve())
    report_path = work / "bench" / "trial-a.json"
    log_path = work / "logs" / "trial-a.log"
    report = json.loads(report_path.read_text())
    assert report == {
        "label": "trial-a",
        "checkout": str(checkout),
        "db": str(db.resolve()),
        "root": str(work / "archive"),
        "date_from": "2026-09-20",
        "date_to": "2026-09-21",
        "command": expected,
        "days": [
            {"date": "2026-09-20", "plan_s": 0.1, "sweeps_s": 0.2,
             "finalize_s": 0.3, "total_s": 0.6, "upsert_calls": 12, "refs_refreshed": 2},
            {"date": "2026-09-21", "plan_s": 0.1, "sweeps_s": 0.2,
             "finalize_s": 0.3, "total_s": 0.6, "upsert_calls": 12, "refs_refreshed": 2},
        ],
        "complete": True,
        "returncode": 0,
        "memory_peak_bytes": None,
    }
    assert log_path.read_text() == "child started\n" + "".join(fake["lines"][1:])


@pytest.mark.parametrize("profile", [False, True], ids=["normal", "profile"])
def test_explicit_dbrel_db_inside_work_dir_is_accepted(harness, monkeypatch, profile):
    bench, work, checkout, db, fake, calls = harness
    monkeypatch.setenv("DBREL_DB", str(db.resolve()))
    fake["lines"] = [_timing("2026-09-20"), _timing("2026-09-21")]
    args = _args(checkout, db, *( ["--profile"] if profile else [] ))

    result = bench.main(args)

    assert result == 0
    assert len(calls) == 1
    assert calls[0].kwargs["env"]["DBREL_DB"] == str(db.resolve())
    report = json.loads((work / "bench" / "trial-a.json").read_text())
    assert report["complete"] is True
    assert report["returncode"] == 0


def test_default_work_dir_stays_with_runner_when_cwd_is_checkout(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    scripts = owner / "scripts"
    scripts.mkdir(parents=True)
    copied_script = scripts / "bench_backfill.py"
    copied_script.write_bytes(SCRIPT.read_bytes())
    checkout = owner / "selected-checkout"
    checkout.mkdir()
    work = owner / "data" / "scratch" / "backfill-speed"
    work.mkdir(parents=True)
    db = work / "scratch.sqlite"
    db.write_bytes(b"isolated benchmark database")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("DBREL_DB", str(db.resolve()))
    fake = {"resume_preexisting": None}
    calls = []

    def popen(argv, **kwargs):
        argv = list(argv)
        done_path = Path(argv[argv.index("--done-file") + 1])
        fake["resume_preexisting"] = done_path.exists()
        done_path.write_text(json.dumps(["2026-09-20"]))
        process = _FakeProcess(argv, lines=[_timing("2026-09-20")], **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    spec = importlib.util.spec_from_file_location("bench_backfill_copied", copied_script)
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bench
    spec.loader.exec_module(bench)

    result = bench.main([
        "--checkout", str(checkout), "--db", str(db),
        "--from", "2026-09-20", "--to", "2026-09-20", "--label", "checkout-cwd",
    ])

    assert result == 0
    assert len(calls) == 1
    assert Path(calls[0].kwargs["cwd"]) == checkout
    assert calls[0].kwargs["env"]["DBREL_DB"] == str(db.resolve())
    assert (work / "bench" / "checkout-cwd.json").is_file()
    assert (work / "logs" / "checkout-cwd.log").is_file()
    assert (work / "checkout-cwd.replayed.json").is_file()


def test_profile_launches_pyspy_as_parent_and_writes_raw_profile_path(harness):
    bench, work, checkout, db, fake, calls = harness
    fake["lines"] = [_timing("2026-09-20"), _timing("2026-09-21")]

    assert bench.main(_args(checkout, db, "--profile")) == 0

    profile = work / "bench" / "trial-a.pyspy.txt"
    assert calls[0].argv == [
        "uvx", "py-spy", "record", "--idle", "--rate", "50", "--format", "raw",
        "-o", str(profile), "--", str(checkout / ".venv" / "bin" / "python"),
        str(SCRIPT), "--profile-child",
        str(work / "bench" / "trial-a.replay-status.json"),
        fake["run_token"],
        str(checkout / ".venv" / "bin" / "dbrel"),
        "backfill", "--from", "2026-09-20", "--to", "2026-09-21", "--restart",
        "--timings", "--root", str(work / "archive"), "--done-file",
        str(work / "trial-a.replayed.json"),
    ]
    assert Path(calls[0].kwargs["cwd"]) == checkout
    assert len(fake["run_token"]) == 32
    assert all(char in "0123456789abcdef" for char in fake["run_token"])
    assert fake["marker_preexisting"] is False
    report = json.loads((work / "bench" / "trial-a.json").read_text())
    assert report["sampler_returncode"] == 0
    assert report["returncode"] == 0


def test_profile_replay_failure_overrides_successful_sampler_and_timings(harness):
    bench, work, checkout, db, fake, calls = harness
    fake["lines"] = [_timing("2026-09-20"), _timing("2026-09-21")]
    fake["replay_returncode"] = 23

    result = bench.main(_args(checkout, db, "--profile"))

    assert result != 0
    assert calls[0].returncode == 0
    report = json.loads((work / "bench" / "trial-a.json").read_text())
    assert report["complete"] is False
    assert report["sampler_returncode"] == 0
    assert report["returncode"] == 23


@pytest.mark.parametrize("marker", ["missing", "stale"])
def test_profile_requires_fresh_matching_child_status_marker(harness, marker):
    bench, work, checkout, db, fake, _calls = harness
    fake["lines"] = [_timing("2026-09-20"), _timing("2026-09-21")]
    fake["marker"] = marker
    status_path = work / "bench" / "trial-a.replay-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({"run_token": "old-success", "returncode": 0}))

    result = bench.main(_args(checkout, db, "--profile"))

    assert result != 0
    assert fake["marker_preexisting"] is False, "stale child status must be removed before launch"
    report = json.loads((work / "bench" / "trial-a.json").read_text())
    assert report["complete"] is False
    assert report["sampler_returncode"] == 0


@pytest.mark.parametrize("stub_exit", [0, 23, "exception"])
def test_profile_child_captures_argv_and_atomic_status(harness, stub_exit, monkeypatch):
    _bench, work, checkout, _db, _fake, _calls = harness
    status_path = work / "status.json"
    token = "unit-run-token"
    argv_probe = work / "console-argv.json"
    stub = checkout / ".venv" / "bin" / "dbrel"
    stub.parent.mkdir(parents=True)
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['BENCH_ARGV_PROBE']).write_text(json.dumps(sys.argv))\n"
        "if os.environ['BENCH_STUB_EXIT'] == 'exception': raise RuntimeError('stub failure')\n"
        "raise SystemExit(int(os.environ['BENCH_STUB_EXIT']))\n"
    )
    env = {
        **os.environ,
        "BENCH_ARGV_PROBE": str(argv_probe),
        "BENCH_STUB_EXIT": str(stub_exit),
    }
    # The harness fakes Popen for benchmark invocations; this test deliberately
    # exercises the local Python wrapper with a harmless console-script stub.
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--profile-child", str(status_path), token,
         str(stub), "backfill", "--from", "2026-09-20"],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
    )

    assert argv_probe.exists()
    assert json.loads(argv_probe.read_text()) == [str(stub), "backfill", "--from", "2026-09-20"]
    status = json.loads(status_path.read_text())
    assert status["run_token"] == token
    if stub_exit == "exception":
        assert result.returncode != 0
        assert status["returncode"] != 0
    else:
        assert result.returncode == stub_exit
        assert status["returncode"] == stub_exit


@pytest.mark.parametrize("label", ["", "../escape", "a/b", "..", ".", "a\\b"])
def test_invalid_labels_are_rejected_without_starting_child(harness, label):
    bench, _work, checkout, db, _fake, calls = harness

    assert bench.main(_args(checkout, db, "--label", label)) != 0
    assert calls == []


@pytest.mark.parametrize(
    "extra",
    ["--from 2026-01-01", "--to 2026-01-02", "--root /tmp/elsewhere",
     "--done-file /tmp/other.json", "--restart", "--timings", "--collect"],
)
def test_extra_args_cannot_override_reserved_workload_options(harness, extra):
    bench, _work, checkout, db, _fake, calls = harness

    assert bench.main(_args(checkout, db, "--extra-args", extra)) != 0
    assert calls == []


@pytest.mark.parametrize("date_from,date_to", [("bad", "2026-09-21"), ("2026-09-22", "2026-09-21")])
def test_invalid_or_reversed_date_windows_are_rejected(harness, date_from, date_to):
    bench, _work, checkout, db, _fake, calls = harness
    args = _args(checkout, db)
    args[args.index("2026-09-20")] = date_from
    args[args.index("2026-09-21")] = date_to

    assert bench.main(args) != 0
    assert calls == []


@pytest.mark.parametrize("live_kind", ["cwd", "checkout", "symlink", "hardlink"])
def test_live_database_paths_and_samefile_aliases_are_rejected(harness, live_kind):
    bench, work, checkout, _scratch_db, _fake, calls = harness
    live = work / "data" / "dbrel.sqlite"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"isolated live-path sentinel")
    if live_kind == "cwd":
        target = live
    elif live_kind == "checkout":
        target = checkout / "data" / "dbrel.sqlite"
        target.parent.mkdir()
        target.write_bytes(b"checkout database sentinel")
    elif live_kind == "symlink":
        target = work / "db-alias.sqlite"
        target.symlink_to(live)
    else:
        target = work / "db-hardlink.sqlite"
        os.link(live, target)

    before = live.read_bytes()
    assert bench.main(_args(checkout, target)) != 0
    assert calls == []
    assert live.read_bytes() == before


@pytest.mark.parametrize("configuration", ["env_absolute", "env_relative", "dotenv", "env_wins"])
def test_configured_data_directory_database_is_protected(harness, monkeypatch, configuration):
    bench, work, checkout, _scratch_db, _fake, calls = harness
    monkeypatch.delenv("DBREL_DATA_DIR", raising=False)
    if configuration == "env_absolute":
        data_dir = work / "external-data"
        monkeypatch.setenv("DBREL_DATA_DIR", str(data_dir))
    elif configuration == "env_relative":
        data_dir = checkout / "relative-data"
        monkeypatch.setenv("DBREL_DATA_DIR", "relative-data")
    elif configuration == "dotenv":
        data_dir = checkout / "dot-env-data"
        (checkout / ".env").write_text("DBREL_DATA_DIR=dot-env-data\n")
    else:
        data_dir = checkout / "environment-wins"
        (checkout / ".env").write_text("DBREL_DATA_DIR=dotenv-data\n")
        monkeypatch.setenv("DBREL_DATA_DIR", "environment-wins")
    configured_db = data_dir / "dbrel.sqlite"
    configured_db.parent.mkdir(parents=True)
    configured_db.write_bytes(b"configured default database sentinel")
    before = configured_db.read_bytes()

    assert bench.main(_args(checkout, configured_db)) != 0
    assert calls == []
    assert configured_db.read_bytes() == before


def _custom_live_db(work, checkout, monkeypatch, source):
    monkeypatch.delenv("DBREL_DB", raising=False)
    if source == "inherited_outside_work":
        live = work.parent / f"persistent-{work.name}" / "dbrel.sqlite"
        monkeypatch.setenv("DBREL_DB", str(live))
    else:
        live = work / "persistent-dbrel.sqlite"
        (checkout / ".env").write_text(f"DBREL_DB={live}\n")
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"custom configured live database")
    return live


@pytest.mark.parametrize("source", ["inherited_outside_work", "dotenv_persistent_in_work"])
@pytest.mark.parametrize("alias_kind", ["direct", "symlink", "hardlink"])
def test_custom_configured_live_database_and_aliases_are_rejected(
    harness, monkeypatch, source, alias_kind
):
    bench, work, checkout, _scratch_db, _fake, calls = harness
    live = _custom_live_db(work, checkout, monkeypatch, source)
    if alias_kind == "direct":
        target = live
    elif alias_kind == "symlink":
        target = work / "custom-live-alias.sqlite"
        target.symlink_to(live)
    else:
        target = work / "custom-live-hardlink.sqlite"
        os.link(live, target)
    before = live.read_bytes()

    assert bench.main(_args(checkout, target)) != 0
    assert calls == []
    assert live.read_bytes() == before


@pytest.mark.parametrize("source", ["inherited_outside_work", "dotenv_persistent_in_work"])
@pytest.mark.parametrize("component", ["db", "wal", "shm", "writer_lock"])
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_outputs_protect_custom_live_database_sidecars_and_lock_aliases(
    harness, monkeypatch, source, component, alias_kind
):
    bench, work, checkout, scratch_db, _fake, calls = harness
    live = _custom_live_db(work, checkout, monkeypatch, source)
    protected = {
        "db": live,
        "wal": Path(f"{live}-wal"),
        "shm": Path(f"{live}-shm"),
        "writer_lock": Path(f"{live}.writer.lock"),
    }[component]
    if protected != live:
        protected.write_bytes(f"{component} bytes".encode())
    report_path = work / "bench" / "trial-a.json"
    report_path.parent.mkdir(parents=True)
    if alias_kind == "symlink":
        report_path.symlink_to(protected)
    else:
        os.link(protected, report_path)
    before = protected.read_bytes()

    assert bench.main(_args(checkout, scratch_db)) != 0
    assert calls == []
    assert protected.read_bytes() == before
    if alias_kind == "symlink":
        assert report_path.is_symlink()


@pytest.mark.parametrize("component", ["db", "wal", "shm", "writer_lock"])
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_benchmark_outputs_cannot_alias_configured_database_files(
    harness, monkeypatch, component, alias_kind
):
    bench, work, checkout, _scratch_db, _fake, calls = harness
    data_dir = work / "configured-data"
    monkeypatch.setenv("DBREL_DATA_DIR", str(data_dir))
    db = data_dir / "dbrel.sqlite"
    db.parent.mkdir()
    db.write_bytes(b"configured database sentinel")
    protected = {
        "db": db,
        "wal": Path(f"{db}-wal"),
        "shm": Path(f"{db}-shm"),
        "writer_lock": Path(f"{db}.writer.lock"),
    }[component]
    if protected != db:
        protected.write_bytes(f"{component} sentinel".encode())
    report_path = work / "bench" / "trial-a.json"
    report_path.parent.mkdir(parents=True)
    if alias_kind == "symlink":
        report_path.symlink_to(protected)
    else:
        os.link(protected, report_path)
    before = protected.read_bytes()

    assert bench.main(_args(checkout, _scratch_db)) != 0
    assert calls == []
    assert protected.read_bytes() == before
    if alias_kind == "symlink":
        assert report_path.is_symlink()


@pytest.mark.parametrize(
    "line",
    [
        "child started\n",
        _timing("2026-09-20") + _timing("2026-09-20"),
        _timing("2026-09-20", plan="nan"),
        _timing("2026-09-20", sweeps="inf"),
        _timing("2026-09-20", finalize="-0.1"),
        _timing("2026-09-20", total="-1"),
    ],
)
def test_missing_duplicate_nonfinite_or_negative_timings_are_incomplete(harness, line):
    bench, work, checkout, db, fake, _calls = harness
    fake["lines"] = [line]

    result = bench.main(_args(checkout, db))

    assert result != 0
    report = json.loads((work / "bench" / "trial-a.json").read_text())
    assert report["complete"] is False


def test_nonzero_child_replaces_stale_success_report_with_incomplete_result(harness):
    bench, work, checkout, db, fake, _calls = harness
    report_path = work / "bench" / "trial-a.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({"label": "trial-a", "complete": True}))
    fake["lines"] = [_timing("2026-09-20"), _timing("2026-09-21")]
    fake["returncode"] = 17

    result = bench.main(_args(checkout, db))

    assert result != 0
    report = json.loads(report_path.read_text())
    assert report["complete"] is False
    assert report["returncode"] == 17
    assert report["days"]


def test_missing_day_from_zero_exit_child_is_incomplete(harness):
    bench, work, checkout, db, fake, _calls = harness
    fake["lines"] = [_timing("2026-09-20")]

    result = bench.main(_args(checkout, db))

    assert result != 0
    report = json.loads((work / "bench" / "trial-a.json").read_text())
    assert report["complete"] is False
    assert report["returncode"] == 0


@pytest.mark.parametrize("done_mode", ["missing", "partial", "extra", "duplicate", "badtypes", "malformed"])
def test_profileless_success_requires_exact_fresh_resume_days(harness, done_mode):
    bench, work, checkout, db, fake, _calls = harness
    fake["lines"] = [_timing("2026-09-20"), _timing("2026-09-21")]
    fake["done_mode"] = done_mode

    result = bench.main(_args(checkout, db))

    assert result != 0
    assert fake["resume_preexisting"] is False
    report = json.loads((work / "bench" / "trial-a.json").read_text())
    assert report["complete"] is False


def test_stale_resume_record_is_removed_before_child_and_replaced_with_exact_dates(harness):
    bench, work, checkout, db, fake, _calls = harness
    stale = work / "trial-a.replayed.json"
    stale.write_text(json.dumps(["2026-09-20", "2026-09-21"]))
    fake["lines"] = [_timing("2026-09-20"), _timing("2026-09-21")]

    assert bench.main(_args(checkout, db)) == 0

    assert fake["resume_preexisting"] is False
    assert json.loads(stale.read_text()) == ["2026-09-20", "2026-09-21"]


def test_zero_data_day_with_valid_timings_and_fresh_completion_record_succeeds(harness):
    bench, work, checkout, db, fake, _calls = harness
    args = _args(checkout, db)
    args[args.index("2026-09-21")] = "2026-09-20"
    fake["lines"] = [_timing("2026-09-20", plan="0", sweeps="0", finalize="0",
                             total="0", upserts="0", refreshed="0")]

    result = bench.main(args)

    assert result == 0
    report = json.loads((work / "bench" / "trial-a.json").read_text())
    assert report["complete"] is True
    assert report["days"] == [{
        "date": "2026-09-20", "plan_s": 0.0, "sweeps_s": 0.0,
        "finalize_s": 0.0, "total_s": 0.0, "upsert_calls": 0, "refs_refreshed": 0,
    }]


def test_memory_peak_annotation_only_updates_existing_report(harness, monkeypatch):
    bench, work, _checkout, _db, _fake, calls = harness
    report_path = work / "bench" / "trial-a.json"
    report_path.parent.mkdir(parents=True)
    original = {"label": "trial-a", "complete": True, "days": [{"date": "2026-09-20"}]}
    report_path.write_text(json.dumps(original))

    result = bench.main(["--label", "trial-a", "--memory-peak", "987654"])

    assert result == 0
    updated = json.loads(report_path.read_text())
    assert updated == {**original, "memory_peak_bytes": 987654}
    assert calls == []


def test_memory_peak_requires_an_existing_report(harness):
    bench, _work, _checkout, _db, _fake, calls = harness

    assert bench.main(["--label", "missing", "--memory-peak", "100"]) != 0
    assert calls == []


@pytest.mark.parametrize("peak", ["-1", "nan", "1.5"])
def test_memory_peak_must_be_a_nonnegative_integer(harness, peak):
    bench, work, _checkout, _db, _fake, _calls = harness
    report_path = work / "bench" / "trial-a.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({"label": "trial-a", "complete": True}))

    assert bench.main(["--label", "trial-a", "--memory-peak", peak]) != 0
    assert json.loads(report_path.read_text()).get("memory_peak_bytes") is None
