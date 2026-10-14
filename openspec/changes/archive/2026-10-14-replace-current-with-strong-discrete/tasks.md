# Tasks (as carried out)

The work ran in `/home/anas/dev/DB-reliability-app`.

## 1. Fair comparison setup

- [x] 1.1 Head-to-head of the experiment model `20261014T091943Z` (trained to 2026-08-22) against live `20261004T213106Z` on 2026-09-20..26. The live model reproduced its `final.json` score. This exposed the training-cutoff gap, so the deploy candidate became a retrain (`docs/live-vs-strong-results.md`)
- [x] 1.2 Build corrected labels for the test week 2026-09-20..26: 205,645 of 2,210,206 changed (`data/ml/live_vs_strong_20261014/corrected/`)

## 2. Retrain up to 2026-09-19

- [x] 2.1 Join corrected labels onto the live model's training rows (96,911,159). Correct the 385,255 rows from 2026-08-21/22 that lacked a corrected label (overlap cross-check: 0 differences)
- [x] 2.2 Train the strong discrete recipe: amplitude 2.0 over 2026-08-19..09-19, 12 epochs, seed 1, the live model's inputs and notice rates. 16 min, peak 4.2 GiB (`scripts/refit_strong_discrete.py`)
- [x] 2.3 Export discrete artifact `20261014T114610Z` (16.2 MB, deployable); discrete ONNX equals torch on the fixture rows
- [x] 2.4 Browser check passes (pinned Chromium, ONNX Runtime Web 1.30.0)

## 3. Head-to-head

- [x] 3.1 Score both on the same 2,210,206 arrivals, using original and corrected labels, raw and displayed, with a paired 7-day bootstrap. On corrected labels, displayed, the retrain wins: within 5 min 89.2% vs 86.8%, average error 2.52 vs 2.82 min, all intervals clear of zero (`docs/live-vs-refit-results.md`)

## 4. Switch and deploy

- [x] 4.1 Copy to `models/v2/20261014T114610Z` (hashes verified) and `promote` (previous: `20261004T213106Z`)
- [x] 4.2 Smoke test on `data/deploy/slim.sqlite`: the board reports the new stamp, the served ONNX is byte-identical, all requests returned 200, there are no notice-rate errors, and forecasts on real trains look sensible. The slim database was not rebuilt, because it has the same notice-rate fingerprint
- [x] 4.3 With the user's confirmation, commit `e8fc543` (model folder and `CURRENT` only)
- [x] 4.4 `fly deploy` from a clean worktree of `e8fc543`. The live site serves `20261014T114610Z`, the ONNX hash matches and the health check is passing

## Rollback (if ever needed)

`promote` back to `20261004T213106Z`, then commit and redeploy. No database step is needed, because both use notice-rate fingerprint `1e57bfee…`.
