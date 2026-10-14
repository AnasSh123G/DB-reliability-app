## Why

**Overall goal:** make the app's delay forecasts more accurate.

The v3 comparison ([archive](../archive/2026-10-14-try-strong-recency-and-time-of-year)) found the
best recipe: the **strong-weighted discrete model**, where the last training month counts up to
3×, trained on corrected labels. The user approved replacing the live model
`20261004T213106Z` with it (2026-10-14).

The candidate from that experiment (`20261014T091943Z`) was trained only up to 2026-08-22,
because September was held out for scoring. The live model was refit up to 2026-09-19, so a
head-to-head between those two would have favoured the live model's newer data. The fix was to
apply the recipe to the live model's own training rows up to 2026-09-19, then compare the two on
a week neither had seen.

## What Changed

- **Retrain:** `20261014T114610Z`, the strong discrete recipe on the live model's exact training
  rows (96,911,159 rows, 2025-11-03..2026-09-19). It uses the live model's inputs and notice
  rates (fingerprint `1e57bfee…`) and corrected labels for every row. The 3× ramp covers
  2026-08-19..09-19, with 12 epochs and no epoch selection.
- **Head-to-head (offline):** both models scored on the locked test week 2026-09-20..26
  (2,210,206 arrivals neither trained on), using original and corrected labels, with a paired
  per-day bootstrap.
- **Switch and deploy:** copied into `models/v2/20261014T114610Z`, promoted, smoke-tested on the
  deployed slim database, committed as `e8fc543`, and deployed with `fly deploy` from a clean
  checkout of that commit.

## Result

On corrected labels, as the app shows forecasts:

| | `20261004T213106Z` (previous live) | `20261014T114610Z` (now live) |
| --- | --- | --- |
| Within 1 min | 60.5% | 63.8% |
| Within 3 min | 79.5% | 82.4% |
| Within 5 min | 86.8% | 89.2% |
| Avg error | 2.82 min | 2.52 min |

All differences hold across the 7 days (95% intervals clear of zero). On the original,
uncorrected labels the previous model leads (88.0% vs 86.6% within 5 min), because it learned
those labels. Details: `docs/live-vs-refit-results.md`.

## Capabilities

### New Capabilities

### Modified Capabilities
- `delay-model`: a candidate replaces `CURRENT` only after a head-to-head against it, with the
  same training cutoff and on unseen arrivals.

## Impact

- `models/v2/20261014T114610Z/`, `models/v2/CURRENT` (commit `e8fc543`); `20261004T213106Z` kept
  for rollback
- `scripts/refit_strong_discrete.py`, `scripts/compare_live_vs_strong.py` (not committed)
- `data/ml/refit_strong_20261014/`, `data/ml/live_vs_strong_20261014/`
- The deployed database is unchanged, because the notice rates are the same
