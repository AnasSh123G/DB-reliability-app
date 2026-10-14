"""Browser vs Python parity gate: one 0.01-minute end-to-end tolerance."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import browser_parity  # noqa: E402


def _case(**overrides):
    case = {"rows": 10, "wrong_shape": 0, "single_unknown_categories_checked": True,
            "max_abs_vs_python": 0.0, "single_vs_batch_max_abs": 0.0}
    case.update(overrides)
    return case


def _report(output="probs", **case_overrides):
    names = ("median", "window3", "window5") if output == "probs" else ("median",)
    return {"parity_protocol": 2, "parity_policy": deepcopy(browser_parity.POLICY), "output": output,
            "cases": {name: _case(**case_overrides) for name in names}}


def test_observed_percentile_float_noise_passes():
    assert browser_parity.report_passes(_report(max_abs_vs_python=0.00452837))


def test_quantile_track_needs_only_median():
    assert browser_parity.report_passes(_report(output="quantiles", max_abs_vs_python=1e-4))


@pytest.mark.parametrize("overrides", [
    {"max_abs_vs_python": 0.02},
    {"max_abs_vs_python": float("nan")},
    {"max_abs_vs_python": None},
    {"single_vs_batch_max_abs": 2e-3},
    {"wrong_shape": 1},
    {"rows": 0},
    {"single_unknown_categories_checked": False},
])
def test_real_differences_fail(overrides):
    assert not browser_parity.report_passes(_report(**overrides))


def test_missing_case_or_stale_policy_fails():
    report = _report()
    del report["cases"]["window5"]
    assert not browser_parity.report_passes(report)
    report = _report()
    report["parity_policy"] = {"version": 1}
    assert not browser_parity.report_passes(report)
