"""Memory accounting counts swap, and cgroup figures only come from a launch unit."""

from __future__ import annotations

import numpy as np

from dbrel.ml.memory import format_memory, memory_report, own_cgroup


def test_total_rises_with_an_allocation():
    before = memory_report()["total"]
    block = np.ones(25_000_000)  # 200 MB, written so the pages are really used
    after = memory_report()["total"]
    assert after - before >= 190_000_000
    assert block[-1] == 1.0


def test_total_is_resident_plus_swapped():
    r = memory_report()
    assert r["total"] == r["rss"] + r["swap"]


def test_group_fields_are_none_outside_a_launch_unit():
    editor = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-code-oss-1.scope\n"
    r = memory_report(proc_cgroup=editor)
    assert r["unit"] is None
    assert r["group_peak"] is None and r["group_current"] is None and r["group_swap"] is None
    assert "group peak" not in format_memory(r)


def test_unrecognised_cgroup_text_is_not_an_error():
    assert own_cgroup("garbage\n") is None
    assert memory_report(proc_cgroup="garbage\n")["unit"] is None


