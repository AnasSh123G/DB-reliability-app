## Why

**Overall goal:** find out whether giving recent training data more weight makes the delay
forecasts more accurate. If it does, the user decides whether the weighted model replaces the
live one.

The experiment trains two otherwise identical sets of models in
`data/ml/recency_training_mixed_20261014`:
- control: every training day counts the same
- weighted: recent days count more

Both are scored on the same September arrivals (2026-09-06 to 2026-09-19). The first attempt
(`recency_training_20261013`) was invalid because weight normalization cancelled the recency
effect, so this is the corrected run.

The queue stopped at the control distribution browser check. That check failed on 0.0045 min of
float noise. [loosen-browser-parity-tolerance](../loosen-browser-parity-tolerance) fixed the
check, and it now passes. The control models are done; the weighted models have not been
trained yet.

## What Changes

- Resume the serial controller. It skips control fits that are already done, and runs the
  browser checks again.
- Train the weighted fine, distribution and discrete models, then run their browser checks.
- Produce the comparison report, `docs/recency-training-results.md`.
- Give the user a plain answer and get their replacement decision.

No code changes are planned. If a job fails, fix only what that failure needs, then resume.

## Capabilities

### New Capabilities

### Modified Capabilities
- `delay-model`: records how the recency comparison reaches a decision.

## Impact

- New model runs under `data/ml/recency_training_mixed_20261014/weighted/runs/`
- `docs/recency-training-results.md` is regenerated
- `models/v2/CURRENT` changes only if the user approves
