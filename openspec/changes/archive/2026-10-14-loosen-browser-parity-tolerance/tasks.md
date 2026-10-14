# Tasks

## 1. Loosen the tolerance (done)

- [x] 1.1 Diagnose the `control-distribution-browser` failure. Result: a 0.00453-min max gap on all three cases (576 rows each, single vs batch 0), with no whole-minute forecast changed. Evidence: `control/runs/distribution/browser-parity-numeric-analysis.json`
- [x] 1.2 Rewrite `scripts/browser_parity.py` to the single rule: forecasts within 0.01 min, single vs batch within 0.001 min, plus rows, shape, unknown-category and required-case checks
- [x] 1.3 Replace `tests/test_browser_parity.py` with small tests: the 0.0045-min noise passes; quantile output needs only median; a 0.02-min gap, NaN, None, a 2e-3 single-vs-batch gap, a wrong shape, zero rows, unchecked categories, a missing case and a stale policy fail
- [x] 1.4 Set the protocol-1 threshold in `scripts/summarize_recency_training.py` to 0.01 min
- [x] 1.5 Run the tests: `pytest tests/test_browser_parity.py tests/test_recency_training_summary.py tests/test_recency_browser_identity.py tests/test_recency_controller_browser.py tests/test_recency_experiment.py` gives 38 passed

## 2. Unblock the queue

- [x] 2.1 Refresh `data/ml/recency_training_mixed_20261014/readiness.json`: rerun the CPU tests and update `sources` to the current fingerprints. Four scripts differ now: `browser_parity.py`, `check_recency_browser.py`, `run_recency_training.py`, `summarize_recency_training.py`. Check that `fingerprints()` matches `readiness['sources']`
- [x] 2.2 Rerun the browser check for `control/runs/distribution` and confirm `actual_frontend_browser.json` reports `passed: true`
- [x] 2.3 Once the tolerance work is done, create a new OpenSpec change with detailed steps to reach the overall goal: finish the control vs weighted recency comparison, report the results, and reach the user's replacement decision

## 3. Finish the comparison

Moved to [finish-recency-comparison](../finish-recency-comparison/tasks.md).

## 4. Close out

- [x] 4.1 Run `openspec validate loosen-browser-parity-tolerance`, then archive the change together with `finish-recency-comparison`
