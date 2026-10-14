# Live vs refit: strong discrete model retrained through 2026-09-19

## Summary

- **On corrected labels, as the app displays them, the refit (B) is better than the live model (A).** Right to within 5 minutes: A 86.78% vs B 89.20% (+2.42 pp; 95% service-date interval [+2.12 pp, +2.63 pp]). Within 1 minute: A 60.50% vs B 63.78% (+3.28 pp, [+3.13 pp, +3.42 pp]). Average error: A 2.82 vs B 2.52 min (-0.307 min, [-0.327, -0.280]).
- **On the original (uncorrected) labels**, the label definition A was trained on: within 5 minutes A 88.01% vs B 86.57% (-1.44 pp, [-1.63 pp, -1.17 pp]); MAE A 2.87 vs B 3.07.
- **What the extra four weeks added** (refit B vs the earlier strong B trained through 2026-08-21; corrected labels, displayed): within 5 min 89.01% → 89.20% (+0.18 pp, [+0.03 pp, +0.31 pp]); MAE 2.56 → 2.52. The refit also uses A's input encoding (vocabulary, scaling, notice rates over 321 dates) instead of the earlier search encoding.

All three models predicted the same 2,210,206 arrivals of 2026-09-20..26, a week none of them trained on.

- **A (live):** `models/v2/20261004T213106Z`, quantile MLP, trained 2025-11-03..2026-09-19 on original labels.
- **B (refit, new):** `data/ml/refit_strong_20261014/artifacts/20261014T114610Z`. The strong discrete recipe (binned classifier, seed 1, 12 epochs, no epoch selection, recency weights rising linearly to 3x over 2026-08-19..2026-09-19) trained on A's exact 96,911,159 rows and inputs, with version-2 dated-plan corrected labels on every row.
- **Earlier B:** `data/ml/recency_training_v3_20261014/strong/runs/discrete/artifacts/20261014T091943Z`, the same recipe trained on corrected labels 2025-11-03..2026-08-21 with the search encoding; reference column only.

'Displayed' applies the app's rule (`shown` in `scripts/summarize_recency_training.py`): below 1 minute shows as 0, otherwise round half up to a whole minute. The headline comparison is B vs A, displayed.

## Scores on corrected labels

| Model | View | Within 1 min | Within 3 min | Within 5 min | MAE (min) | Inside p25–p75 |
|---|---|---:|---:|---:|---:|---:|
| A live (20261004T213106Z) | raw | 48.03% | 76.34% | 85.08% | 3.0897 | 52.02% |
| A live (20261004T213106Z) | displayed | 60.50% | 79.51% | 86.78% | 2.8231 | 52.02% |
| B refit (new) | raw | 63.76% | 82.39% | 89.16% | 2.5206 | 72.90% |
| B refit (new) | displayed | 63.78% | 82.42% | 89.20% | 2.5160 | 72.90% |
| earlier B (20261014T091943Z) | raw | 63.49% | 82.12% | 88.91% | 2.5808 | 71.41% |
| earlier B (20261014T091943Z) | displayed | 63.59% | 82.24% | 89.01% | 2.5590 | 71.41% |

### B (refit) − A (live), paired service-date bootstrap (corrected labels)

2,000 paired resamples of the 7 service dates (seed 4231), percentile 95% interval (`summarize_recency_training.paired_day_bootstrap`). Positive within-rate and negative MAE differences favour the refit.

| View | Metric | A | B (refit) | Difference | 95% interval |
|---|---|---:|---:|---:|---|
| raw | within 1 min | 48.03% | 63.76% | +15.74 pp | [+15.29 pp, +16.12 pp] |
| raw | within 3 min | 76.34% | 82.39% | +6.05 pp | [+5.57 pp, +6.40 pp] |
| raw | within 5 min | 85.08% | 89.16% | +4.08 pp | [+3.73 pp, +4.36 pp] |
| raw | MAE (min) | 3.0897 | 2.5206 | -0.5691 | [-0.5870, -0.5446] |
| displayed | within 1 min | 60.50% | 63.78% | +3.28 pp | [+3.13 pp, +3.42 pp] |
| displayed | within 3 min | 79.51% | 82.42% | +2.92 pp | [+2.64 pp, +3.12 pp] |
| displayed | within 5 min | 86.78% | 89.20% | +2.42 pp | [+2.12 pp, +2.63 pp] |
| displayed | MAE (min) | 2.8231 | 2.5160 | -0.3071 | [-0.3267, -0.2797] |

### B (refit) − earlier B (corrected labels)

2,000 paired resamples of the 7 service dates (seed 4231), percentile 95% interval (`summarize_recency_training.paired_day_bootstrap`). Positive within-rate and negative MAE differences favour the refit.

| View | Metric | earlier B | B (refit) | Difference | 95% interval |
|---|---|---:|---:|---:|---|
| raw | within 1 min | 63.49% | 63.76% | +0.27 pp | [+0.03 pp, +0.56 pp] |
| raw | within 3 min | 82.12% | 82.39% | +0.26 pp | [+0.04 pp, +0.52 pp] |
| raw | within 5 min | 88.91% | 89.16% | +0.26 pp | [+0.07 pp, +0.45 pp] |
| raw | MAE (min) | 2.5808 | 2.5206 | -0.0603 | [-0.1099, -0.0283] |
| displayed | within 1 min | 63.59% | 63.78% | +0.19 pp | [-0.00 pp, +0.46 pp] |
| displayed | within 3 min | 82.24% | 82.42% | +0.19 pp | [-0.00 pp, +0.38 pp] |
| displayed | within 5 min | 89.01% | 89.20% | +0.18 pp | [+0.03 pp, +0.31 pp] |
| displayed | MAE (min) | 2.5590 | 2.5160 | -0.0430 | [-0.0662, -0.0227] |

### By service date (corrected labels, displayed)

| Date | Rows | A within 5 | B within 5 | earlier B within 5 | A MAE | B MAE | earlier B MAE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-09-20 | 260,399 | 89.94% | 91.53% | 91.25% | 2.246 | 2.015 | 2.116 |
| 2026-09-21 | 284,748 | 86.94% | 89.14% | 89.33% | 2.763 | 2.474 | 2.478 |
| 2026-09-22 | 357,004 | 86.74% | 89.42% | 89.03% | 2.901 | 2.567 | 2.633 |
| 2026-09-23 | 348,876 | 86.63% | 89.20% | 88.96% | 2.836 | 2.513 | 2.548 |
| 2026-09-24 | 343,087 | 86.15% | 88.70% | 88.55% | 3.009 | 2.675 | 2.710 |
| 2026-09-25 | 334,760 | 84.91% | 87.70% | 87.34% | 3.029 | 2.699 | 2.749 |
| 2026-09-26 | 281,332 | 86.94% | 89.19% | 89.23% | 2.833 | 2.550 | 2.562 |

### Arrivals whose label the correction changed (status 2), corrected labels, displayed

| Model | Rows | Within 1 min | Within 5 min | MAE (min) |
|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | 205,645 | 36.94% | 65.08% | 5.780 |
| B refit (new) | 205,645 | 58.37% | 85.86% | 3.099 |
| earlier B (20261014T091943Z) | 205,645 | 59.34% | 86.41% | 3.119 |

### Arrivals whose label is the same in both label sets, corrected labels, displayed

| Model | Rows | Within 1 min | Within 5 min | MAE (min) |
|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | 2,004,561 | 62.92% | 89.01% | 2.520 |
| B refit (new) | 2,004,561 | 64.34% | 89.54% | 2.456 |
| earlier B (20261014T091943Z) | 2,004,561 | 64.02% | 89.28% | 2.502 |

## Scores on original labels

| Model | View | Within 1 min | Within 3 min | Within 5 min | MAE (min) | Inside p25–p75 |
|---|---|---:|---:|---:|---:|---:|
| A live (20261004T213106Z) | raw | 50.51% | 79.03% | 87.62% | 2.5810 | 54.16% |
| A live (20261004T213106Z) | displayed | 61.67% | 80.76% | 88.01% | 2.8684 | 54.16% |
| B refit (new) | raw | 61.00% | 79.61% | 86.57% | 3.0643 | 70.11% |
| B refit (new) | displayed | 60.97% | 79.60% | 86.57% | 3.0715 | 70.11% |
| earlier B (20261014T091943Z) | raw | 60.70% | 79.33% | 86.28% | 3.1022 | 68.53% |
| earlier B (20261014T091943Z) | displayed | 60.64% | 79.28% | 86.23% | 3.1239 | 68.53% |

### B (refit) − A (live), paired service-date bootstrap (original labels)

2,000 paired resamples of the 7 service dates (seed 4231), percentile 95% interval (`summarize_recency_training.paired_day_bootstrap`). Positive within-rate and negative MAE differences favour the refit.

| View | Metric | A | B (refit) | Difference | 95% interval |
|---|---|---:|---:|---:|---|
| raw | within 1 min | 50.51% | 61.00% | +10.49 pp | [+10.11 pp, +10.81 pp] |
| raw | within 3 min | 79.03% | 79.61% | +0.59 pp | [+0.48 pp, +0.70 pp] |
| raw | within 5 min | 87.62% | 86.57% | -1.05 pp | [-1.18 pp, -0.88 pp] |
| raw | MAE (min) | 2.5810 | 3.0643 | +0.4834 | [+0.4616, +0.5002] |
| displayed | within 1 min | 61.67% | 60.97% | -0.70 pp | [-1.06 pp, -0.24 pp] |
| displayed | within 3 min | 80.76% | 79.60% | -1.16 pp | [-1.36 pp, -0.88 pp] |
| displayed | within 5 min | 88.01% | 86.57% | -1.44 pp | [-1.63 pp, -1.17 pp] |
| displayed | MAE (min) | 2.8684 | 3.0715 | +0.2031 | [+0.1685, +0.2281] |

### B (refit) − earlier B (original labels)

2,000 paired resamples of the 7 service dates (seed 4231), percentile 95% interval (`summarize_recency_training.paired_day_bootstrap`). Positive within-rate and negative MAE differences favour the refit.

| View | Metric | earlier B | B (refit) | Difference | 95% interval |
|---|---|---:|---:|---:|---|
| raw | within 1 min | 60.70% | 61.00% | +0.30 pp | [+0.06 pp, +0.55 pp] |
| raw | within 3 min | 79.33% | 79.61% | +0.29 pp | [+0.15 pp, +0.43 pp] |
| raw | within 5 min | 86.28% | 86.57% | +0.30 pp | [+0.19 pp, +0.39 pp] |
| raw | MAE (min) | 3.1022 | 3.0643 | -0.0379 | [-0.0593, -0.0012] |
| displayed | within 1 min | 60.64% | 60.97% | +0.33 pp | [+0.09 pp, +0.58 pp] |
| displayed | within 3 min | 79.28% | 79.60% | +0.32 pp | [+0.20 pp, +0.46 pp] |
| displayed | within 5 min | 86.23% | 86.57% | +0.33 pp | [+0.26 pp, +0.41 pp] |
| displayed | MAE (min) | 3.1239 | 3.0715 | -0.0524 | [-0.0621, -0.0430] |

### By service date (original labels, displayed)

| Date | Rows | A within 5 | B within 5 | earlier B within 5 | A MAE | B MAE | earlier B MAE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-09-20 | 260,399 | 91.07% | 90.39% | 90.17% | 2.345 | 2.453 | 2.485 |
| 2026-09-21 | 284,748 | 88.23% | 86.87% | 86.34% | 2.798 | 2.981 | 3.059 |
| 2026-09-22 | 357,004 | 88.08% | 86.45% | 86.05% | 2.925 | 3.158 | 3.211 |
| 2026-09-23 | 348,876 | 87.91% | 86.16% | 85.89% | 2.856 | 3.097 | 3.146 |
| 2026-09-24 | 343,087 | 87.49% | 85.79% | 85.49% | 3.018 | 3.247 | 3.311 |
| 2026-09-25 | 334,760 | 86.08% | 84.61% | 84.25% | 3.076 | 3.301 | 3.348 |
| 2026-09-26 | 281,332 | 87.92% | 86.64% | 86.42% | 2.939 | 3.108 | 3.147 |

### Arrivals whose label the correction changed (status 2), original labels, displayed

| Model | Rows | Within 1 min | Within 5 min | MAE (min) |
|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | 205,645 | 49.53% | 78.29% | 6.266 |
| B refit (new) | 205,645 | 28.16% | 57.59% | 9.069 |
| earlier B (20261014T091943Z) | 205,645 | 27.66% | 56.56% | 9.190 |

### Arrivals whose label is the same in both label sets, original labels, displayed

| Model | Rows | Within 1 min | Within 5 min | MAE (min) |
|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | 2,004,561 | 62.92% | 89.01% | 2.520 |
| B refit (new) | 2,004,561 | 64.34% | 89.54% | 2.456 |
| earlier B (20261014T091943Z) | 2,004,561 | 64.02% | 89.28% | 2.502 |

## How the refit was made

- **Rows and inputs:** A's final fit set (`data/ml/full_year/prepared/final-2026-09-26-robust-no-doy-mc20-rd56-fit`, 96,911,159 rows, service dates 2025-11-03..2026-09-18; the training split's finalization cutoff of 2026-09-19 17:07 UTC leaves no 2026-09-19 rows) with A's vocabulary, prep and notice-rate record (fingerprint `1e57bfee…`, 321 dates 2025-11-03..2026-09-19, re-verified against the read-only database). Feature files are A's, byte for byte (symlinked).
- **Labels:** version-2 dated-plan corrections joined on service date + stop ID. 96,525,904 rows take the existing search correction (`data/ml/trip_schedule_correction_20261013`); 385,255 rows were corrected in this run (the part of 2026-08-21 and all of 2026-08-22 that the search split's earlier finalization cutoff left out) with the unchanged producer logic and the d-2..d+2 mirror-folder selection. Where both corrections cover a row they agree exactly (2026-08-21: 173,642 rows, 0 label and 0 status differences; 2026-08-22: 0 rows, 0 label and 0 status differences). Rows not corrected (status ≠ 2) carry the original target exactly. Labels changed vs original: 5,358,969 rows. Status counts: {'0': 52414, '1': 61279699, '2': 5358969, '4': 198399, '5': 30021678}.
- **This run's correction counts:** 2026-08-21: {"source_prepared_rows": 301937, "target_tts_rows": 301937, "source_verified": 299947, "corrected": 27793, "corrected_negative_to_nonnegative": 2823, "corrected_outside_previous_120min_bound": 0, "plan_seen_before_corrected_5h_cutoff": 0, "missing_plan": 211, "raw_changed_not_matched": 1779}; 2026-08-22: {"source_prepared_rows": 256960, "target_tts_rows": 256960, "missing_plan": 201, "source_verified": 254907, "corrected": 24864, "corrected_negative_to_nonnegative": 3454, "corrected_outside_previous_120min_bound": 0, "plan_seen_before_corrected_5h_cutoff": 0, "raw_changed_not_matched": 1852}.
- **Training:** the pinned `train_recency_delay.training` with `WeightedSource` (permuted pools of 8 blocks × 262,144 rows, fixed full-dataset mean weight 1.0892), recency policy 2026-08-19..2026-09-19 amplitude 2.0, `configuration('distribution')`, 12 epochs, epoch 12 used (no selection). 16.1 min on the ROCm GPU; cgroup memory peak 4.21 GiB (cap 8 GiB), GPU peak 762 MiB.
- **Export:** discrete p25/p50/p75 baked into ONNX by `check_recency_discrete_delay.DiscreteExport`, gated as that script gates it: ONNX equals torch exactly on the 576 fixture rows (max difference 0.0). Extra diagnostic on 262,144 spread training rows: distribution ONNX probabilities within 1.9e-05 of torch (tolerance 1e-4); 21 of 786,432 discrete quantile values (6 medians) differ from torch by 1 minute, where a cumulative probability sits within float rounding of a quartile; the ONNX output always equals the NumPy discrete rule applied to the ONNX probabilities (0 differences). Artifact `data/ml/refit_strong_20261014/artifacts/20261014T114610Z`, 16,209,594 bytes, role deployable.
- **Browser check** (pinned Chromium, actual frontend predictor, 0.01-min tolerance): distribution: passed (ONNX Runtime Web 1.30.0, median max |web−python| 9.75e-03 min, window3 max |web−python| 1.64e-03 min, window5 max |web−python| 1.64e-03 min); discrete: passed (ONNX Runtime Web 1.30.0, median max |web−python| 0.00e+00 min).
- **Check on A:** re-run on the test cache it reproduces final.json (within 5 difference 4.5e-07, MAE 8.0e-09) and the earlier head-to-head's A predictions (max difference 9.5e-07).
- B (refit) reads exactly the same encoded test inputs as A (identical vocabulary, prep and notice record). Earlier-B predictions come from `data/ml/live_vs_strong_20261014/predictions` (hash-checked).
- **Corrected test labels:** `data/ml/live_vs_strong_20261014/corrected/` (changed on 205,645 arrivals).

## Caveats

- Seven service dates only; the bootstrap clusters by date, so intervals are rough and say nothing about other weeks.
- Nothing was selected on held-out data for the refit: the recipe and the 12 epochs come from the earlier strong run. The per-epoch monitor numbers in the run directory are in-sample.
- Original labels favour A's training definition; corrected labels favour B's.
- The recency policy ends on 2026-09-19 (the training split's last date) although the last date with finalized rows is 2026-09-18; the earlier strong run ended its policy on its last date with rows. The effect is a one-day shift of a linear ramp (about 1/31 of the 2x ramp).
- The extra-month comparison (refit vs earlier B) changes two things at once: four more weeks of data and A's input encoding instead of the search encoding.
- B outputs whole-minute quantiles; A's are continuous. The displayed view is the fair comparison for what users see.
- Historical five-hour-cutoff replay with present-day timetable patterns, not a logged prospective forecast.
- Nothing in production changed: CURRENT, the deployed database and other experiments are untouched.
