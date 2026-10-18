"""The memory check measures and projects; the launch gate refuses bake-offs that would not fit."""

from __future__ import annotations

import json

import pytest

from dbrel.ml.launch import LaunchError
from dbrel.ml.memcheck import RESULT_NAME, gate, parse_size, project

from test_ml_bakeoff import env  # noqa: F401 - the fixture database and dataset

GIB = 2**30
BAKEOFF = ["dbrel", "ml", "bakeoff", "--resume", "--libraries", "catboost,xgboost"]


def test_projection_is_linear_in_rows_plus_growth():
    probes = [{"rows": 1_000, "peak": 10 * GIB, "rounds": 100, "growth_per_round": 0.0, "killed": False},
              {"rows": 2_000, "peak": 12 * GIB, "rounds": 100, "growth_per_round": 0.0, "killed": False}]
    assert project(probes, 5_000, max_rounds=100) == 18 * GIB
    probes[1]["growth_per_round"] = 1_000_000
    assert project(probes, 2_000, max_rounds=1_100) == 12 * GIB + 1_000 * 1_000_000
    probes[0]["killed"] = True
    assert project(probes, 5_000, max_rounds=100) is None


def _fake_check(dataset_dir, peaks: dict, *, built_at):
    libraries = {}
    for library, gib in peaks.items():
        libraries[library] = {"kinds": {"corner": {
            "tuning_peak": int(4 * GIB),
            "projected_final_peak": {"dep": None if gib is None else int(gib * GIB),
                                     "arr": None if gib is None else int(gib * GIB * 0.9)},
        }}}
    (dataset_dir / RESULT_NAME).write_text(json.dumps({"dataset_built_at": built_at,
                                                       "libraries": libraries}))


def test_gate_refuses_a_projection_above_the_cap(env):  # noqa: F811
    built_at = json.loads((env / "ml" / "dataset.json").read_text())["built_at"]
    _fake_check(env / "ml", {"catboost": 17.0, "xgboost": 6.0}, built_at=built_at)
    with pytest.raises(LaunchError) as err:
        gate([BAKEOFF], "18G", env / "ml")
    message = str(err.value)
    assert "catboost" in message and "17.0 GiB" in message and "18G" in message
    assert "xgboost" not in message.split("would not fit")[1]
    gate([BAKEOFF], "24G", env / "ml")  # 17 * 1.15 = 19.6 GiB fits under 24


def test_gate_treats_a_probe_killed_at_the_cap_as_not_fitting(env):  # noqa: F811
    built_at = json.loads((env / "ml" / "dataset.json").read_text())["built_at"]
    _fake_check(env / "ml", {"catboost": None, "xgboost": 1.0}, built_at=built_at)
    with pytest.raises(LaunchError, match="catboost: a probe hit the cap"):
        gate([BAKEOFF], "18G", env / "ml")


def test_sizes_parse_like_systemd():
    assert parse_size("18G") == 18 * GIB and parse_size("300M") == 300 * 2**20
