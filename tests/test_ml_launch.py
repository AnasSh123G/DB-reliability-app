"""The launcher's command building and bookkeeping (the systemd side is checked by hand)."""

from __future__ import annotations

import pytest

from dbrel.ml.launch import (
    LaunchError,
    build_command,
    unit_for,
)


def test_unit_names_are_prefixed_and_validated():
    assert unit_for("bakeoff") == "dbrel-ml-bakeoff.service"
    for bad in ("", "Bakeoff", "a b", "x;rm", "-x"):
        with pytest.raises(LaunchError):
            unit_for(bad)


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


def test_a_cap_above_free_memory_is_refused():
    from dbrel.ml.launch import check_cap_fits

    check_cap_fits("12G", 14 * 2**30)
    with pytest.raises(LaunchError, match=r"only 15\.4 GiB is available -- use --memory 14G"):
        check_cap_fits("18G", int(15.4 * 2**30))


