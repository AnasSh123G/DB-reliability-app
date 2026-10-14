## Why

The delay-model spec requires every report to score the two-sided TabM artifact
`20260924T183944Z` and forbids deleting it. That artifact is gone: it isn't on disk and isn't in
git history, so the requirement can't be met. The live model is now `20261014T114610Z`, and its
predecessor `20261004T213106Z` stays in `models/v2` for rollback.

## What Changes

- Remove the TabM clause and its scenario from "Replacement depends on per-arrival evidence and
  deployment approval". Reports compare candidate and incumbent only.
- Nothing else changes. The live-accuracy-study spec uses "TabM" as a loose name for the
  deployed model; it refers to whatever is current, not this artifact.

## Capabilities

### New Capabilities

### Modified Capabilities
- `delay-model`: the replacement requirement no longer names a fixed reference artifact.

## Impact

Spec text only. No code reads that artifact.
