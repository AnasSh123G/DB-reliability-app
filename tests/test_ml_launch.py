"""The launcher's command building and bookkeeping (the systemd side is checked by hand)."""

from __future__ import annotations

import pytest

from dbrel.ml.launch import (
    LaunchError,
    build_command,
    is_running,
    latest_log,
    split_chain,
    unit_for,
)


def test_unit_names_are_prefixed_and_validated():
    assert unit_for("bakeoff") == "dbrel-ml-bakeoff.service"
    for bad in ("", "Bakeoff", "a b", "x;rm", "-x"):
        with pytest.raises(LaunchError):
            unit_for(bad)


def test_chain_splits_on_the_separator():
    assert split_chain(["a", "1", "::", "b", "2"]) == [["a", "1"], ["b", "2"]]
    assert split_chain(["a"]) == [["a"]]
    with pytest.raises(LaunchError):
        split_chain(["a", "::"])


def test_command_is_capped_without_swap_and_kept_after_exit(tmp_path):
    cmd = build_command("dbrel-ml-t.service", [["sleep", "1"]], memory="1G", oom_policy="stop",
                        workdir=tmp_path, log=tmp_path / "t.log")
    joined = " ".join(cmd)
    assert cmd[:2] == ["systemd-run", "--user"]
    assert "--slice=background.slice" in cmd
    for prop in ("MemoryMax=1G", "MemorySwapMax=0", "OOMPolicy=stop", "RemainAfterExit=yes",
                 f"WorkingDirectory={tmp_path}", f"StandardOutput=append:{tmp_path / 't.log'}"):
        assert prop in cmd, prop
    assert "--collect" not in joined and "--scope" not in joined
    assert cmd[cmd.index("--") + 1].endswith("/sleep")


def test_several_steps_run_through_the_chain_runner(tmp_path):
    cmd = build_command("dbrel-ml-t.service", [["sleep", "1"], ["true"]], memory="1G",
                        oom_policy="stop", workdir=tmp_path, log=tmp_path / "t.log")
    tail = cmd[cmd.index("--") + 1:]
    assert tail[1:3] == ["-m", "dbrel.ml.chain"]


def test_latest_log_ignores_other_jobs_with_a_shared_prefix(tmp_path):
    for name in ("bakeoff-20260924T100000Z.log", "bakeoff-20260924T120000Z.log",
                 "bakeoff-tabm-20260924T130000Z.log"):
        (tmp_path / name).write_text("")
    assert latest_log(tmp_path, "bakeoff").name == "bakeoff-20260924T120000Z.log"
    assert latest_log(tmp_path, "nothing") is None


def test_a_finished_remain_after_exit_unit_is_not_running():
    assert is_running({"ActiveState": "active", "SubState": "running"})
    assert not is_running({"ActiveState": "active", "SubState": "exited"})
    assert not is_running({"ActiveState": "failed", "SubState": "failed"})
    assert not is_running({"ActiveState": "inactive", "SubState": "dead"})


def test_an_ignored_memory_check_is_written_into_the_log(tmp_path, monkeypatch):
    import subprocess

    import dbrel.ml.launch as launch_mod
    from dbrel.ml.launch import launch

    calls = []
    monkeypatch.setattr(launch_mod, "available_memory", lambda: 64 * 2**30)
    monkeypatch.setattr(launch_mod, "unit_state", lambda unit: {"LoadState": "not-found"})
    monkeypatch.setattr(launch_mod, "_systemctl", lambda *a: None)
    monkeypatch.setattr(launch_mod.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    unit, log = launch("t", ["sleep", "1"], workdir=tmp_path, logs=tmp_path, memory="1G",
                       note="memory check IGNORED (--ignore-memory-check)")
    assert "memory check IGNORED" in log.read_text()
    assert calls and calls[0][0] == "systemd-run"


def test_the_gate_can_refuse_before_anything_starts(tmp_path, monkeypatch):
    import dbrel.ml.launch as launch_mod
    from dbrel.ml.launch import launch

    def refuse(steps, memory):
        raise LaunchError("would not fit")

    monkeypatch.setattr(launch_mod, "available_memory", lambda: 64 * 2**30)
    monkeypatch.setattr(launch_mod.subprocess, "run", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(LaunchError, match="would not fit"):
        launch("t", ["sleep", "1"], workdir=tmp_path, logs=tmp_path, memory="1G", gate=refuse)
    assert not list(tmp_path.glob("*.log"))


def test_the_cli_only_skips_the_gate_when_told(monkeypatch):
    from typer.testing import CliRunner

    import dbrel.ml.launch as launch_mod
    from dbrel.cli import app

    seen = {}
    monkeypatch.setattr(launch_mod, "launch",
                        lambda name, command, **kw: seen.update(kw) or ("u", launch_mod.Path("/x.log")))
    runner = CliRunner()
    assert runner.invoke(app, ["ml", "launch", "t", "--", "sleep", "1"]).exit_code == 0
    assert seen["gate"] is not None and seen["note"] is None
    assert runner.invoke(app, ["ml", "launch", "t", "--ignore-memory-check", "--", "sleep", "1"]).exit_code == 0
    assert seen["gate"] is None and "IGNORED" in seen["note"]


def test_a_cap_above_free_memory_is_refused():
    from dbrel.ml.launch import check_cap_fits

    check_cap_fits("12G", 14 * 2**30)
    with pytest.raises(LaunchError, match=r"only 15\.4 GiB is available -- use --memory 14G"):
        check_cap_fits("18G", int(15.4 * 2**30))


def test_a_venv_interpreter_symlink_is_not_followed(tmp_path, monkeypatch):
    from dbrel.ml.launch import resolve_executable

    real = tmp_path / "base-python"
    real.write_text("")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    link = tmp_path / "venv" / "bin" / "python"
    link.symlink_to(real)
    monkeypatch.chdir(tmp_path)
    assert resolve_executable("venv/bin/python") == str(link)
