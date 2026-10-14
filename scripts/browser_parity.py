"""Browser vs Python parity: one end-to-end tolerance on the forecast minutes.

Browser WASM and native CPU arithmetic differ by tiny float amounts; percentiles
read from thin probability bins amplify that to fractions of a second. The app
shows whole minutes, so 0.01 min (0.6 s) separates float noise from export bugs.
"""
from __future__ import annotations

import math

POLICY = {'version': 2, 'max_abs_vs_python_min': .01, 'single_vs_batch_max_abs_min': 1e-3}


def bounded(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum


def case_passes(case):
    return (isinstance(case, dict)
            and type(case.get('rows')) is int and case['rows'] > 0
            and case.get('wrong_shape') == 0
            and case.get('single_unknown_categories_checked') is True
            and bounded(case.get('max_abs_vs_python'), POLICY['max_abs_vs_python_min'])
            and bounded(case.get('single_vs_batch_max_abs'), POLICY['single_vs_batch_max_abs_min']))


def report_passes(report):
    if not isinstance(report, dict) or report.get('parity_protocol') != 2 or report.get('parity_policy') != POLICY:
        return False
    cases = report.get('cases')
    expected = {'median', 'window3', 'window5'} if report.get('output') == 'probs' else {'median'}
    return isinstance(cases, dict) and set(cases) == expected and all(map(case_passes, cases.values()))
