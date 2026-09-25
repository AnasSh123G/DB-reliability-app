"""The memory check measures and projects; the launch gate refuses bake-offs that would not fit."""

from __future__ import annotations

import json

import pytest

from dbrel.ml.launch import LaunchError
from dbrel.ml.memcheck import RESULT_NAME, gate, needed_bytes, parse_size, project, run_memory_check

from test_ml_bakeoff import env  # noqa: F401 - the fixture database and dataset

GIB = 2**30
BAKEOFF = ["dbrel", "ml", "bakeoff", "--resume", "--libraries", "catboost,xgboost"]


def test_the_check_writes_every_field(env):  # noqa: F811
    out = run_memory_check(env / "ml", ("catboost", "xgboost"), threads=2, log=lambda m: None,
                           sizes=(200, 400), tuning_rows=300, rounds=10, max_rounds=30)
    saved = json.loads((env / "ml" / RESULT_NAME).read_text())
    assert saved == json.loads(json.dumps(out))
    assert saved["dataset_built_at"] == json.loads((env / "ml" / "dataset.json").read_text())["built_at"]
    for library in ("catboost", "xgboost"):
        corner = saved["libraries"][library]["kinds"]["corner"]
        assert set(corner["probes"]) == {"tuning", "dep_200", "dep_400", "arr_200", "arr_400"}
        for probe in corner["probes"].values():
            assert not probe["killed"]
            assert probe["peak"] >= probe["base_rss"] > 0
            assert probe["swap"] == 0 or probe["swap"] >= 0
            assert probe["seconds_per_round"] > 0
        assert corner["tuning_peak"] > 0
        assert corner["projected_final_peak"]["dep"] > 0 and corner["projected_final_peak"]["arr"] > 0
        assert set(corner["full_train_rows"]) == {"dep", "arr"}


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


def test_gate_needs_a_check_for_this_dataset(env):  # noqa: F811
    with pytest.raises(LaunchError, match="no memory check"):
        gate([BAKEOFF], "18G", env / "ml")
    _fake_check(env / "ml", {"catboost": 1.0, "xgboost": 1.0}, built_at=-1)
    with pytest.raises(LaunchError, match="different dataset"):
        gate([BAKEOFF], "18G", env / "ml")


def test_gate_treats_a_probe_killed_at_the_cap_as_not_fitting(env):  # noqa: F811
    built_at = json.loads((env / "ml" / "dataset.json").read_text())["built_at"]
    _fake_check(env / "ml", {"catboost": None, "xgboost": 1.0}, built_at=built_at)
    with pytest.raises(LaunchError, match="catboost: a probe hit the cap"):
        gate([BAKEOFF], "18G", env / "ml")


def test_gate_ignores_finished_libraries_and_other_commands(env):  # noqa: F811
    (env / "ml" / "runs" / "catboost").mkdir(parents=True)
    (env / "ml" / "runs" / "catboost" / "done").write_text("")
    built_at = json.loads((env / "ml" / "dataset.json").read_text())["built_at"]
    _fake_check(env / "ml", {"catboost": 99.0, "xgboost": 2.0}, built_at=built_at)
    gate([BAKEOFF], "18G", env / "ml")  # catboost is done: its projection no longer matters
    gate([["python", "scripts/tune_tabm.py"]], "1G", env / "ml")


def test_tuned_parameters_decide_once_tuning_is_done():
    entry = {"kinds": {
        "corner": {"tuning_peak": 5 * GIB, "projected_final_peak": {"dep": 30 * GIB, "arr": 29 * GIB}},
        "tuned": {"tuning_peak": 3 * GIB, "projected_final_peak": {"dep": 9 * GIB, "arr": 8 * GIB}},
    }}
    assert needed_bytes(entry, tuning_done=False) == (30 * GIB, "corner")
    assert needed_bytes(entry, tuning_done=True) == (9 * GIB, "tuned")


def test_sizes_parse_like_systemd():
    assert parse_size("18G") == 18 * GIB and parse_size("300M") == 300 * 2**20
