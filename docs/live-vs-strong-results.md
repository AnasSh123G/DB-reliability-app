# Live vs strong: delay-model head-to-head on the locked test week

## Summary

- **Original labels, as displayed in the app:** A (live) is right to within 5 minutes more often — A 88.01% vs B 86.23% (-1.78 pp, 95% day interval excludes zero). Within 1 minute: A 61.67% vs B 60.64% (-1.03 pp). Average error: A 2.87 vs B 3.12 min (+0.255).
- **Corrected labels, as displayed in the app:** B (strong) is right to within 5 minutes more often — A 86.78% vs B 89.01% (+2.23 pp, 95% day interval excludes zero). Within 1 minute: A 60.50% vs B 63.59% (+3.09 pp). Average error: A 2.82 vs B 2.56 min (-0.264).
- **Where the gap comes from:** on the 2,004,561 arrivals whose label is identical in both sets (90.70%), the models are close — within 5 min A 89.01% vs B 89.28%, MAE A 2.52 vs B 2.50. Almost the whole difference is the 205,645 arrivals the correction re-references to the dated plan: each model is far better on the label definition it was trained on, so which model 'wins' depends mainly on which label definition is taken as the truth.

Both models predicted the same 2,210,206 arrivals of 2026-09-20..26, a week neither trained on. A is the live model (`models/v2/20261004T213106Z`, trained 2025-11-03..2026-09-19 on uncorrected labels). B is the v3 strong recency candidate (`data/ml/recency_training_v3_20261014/strong/runs/discrete/artifacts/20261014T091943Z`, trained 2025-11-03..2026-08-22 on corrected labels with 3x recency weighting — four fewer weeks of history than A). The original labels favour A, which learned the same uncorrected label definition; corrected labels follow the definition B was trained on.

'Displayed' applies the app's rule (`shown` in `scripts/summarize_recency_training.py`): an estimate below 1 minute shows as 0, otherwise it rounds half up to a whole minute. B already outputs whole-minute quantiles, so its raw and displayed points differ only where it predicts negative minutes.

## Scores on original labels

| Model | View | Within 1 min | Within 3 min | Within 5 min | MAE (min) |
|---|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | raw | 50.51% | 79.03% | 87.62% | 2.5810 |
| A live (20261004T213106Z) | displayed | 61.67% | 80.76% | 88.01% | 2.8684 |
| B strong (20261014T091943Z) | raw | 60.70% | 79.33% | 86.28% | 3.1022 |
| B strong (20261014T091943Z) | displayed | 60.64% | 79.28% | 86.23% | 3.1239 |

Share of arrivals inside the p25–p75 band (ideal 50%): A 54.16%, B 68.53%.

### B − A, paired service-date bootstrap (original labels)

2,000 paired resamples of the 7 service dates (seed 4231), percentile 95% interval, as in `summarize_recency_training.paired_day_bootstrap`. Positive within-rate differences and negative MAE differences favour B.

| View | Metric | A | B | B − A | 95% interval |
|---|---|---:|---:|---:|---|
| raw | within 1 min | 50.51% | 60.70% | +10.19 pp | [+9.89 pp, +10.53 pp] |
| raw | within 3 min | 79.03% | 79.33% | +0.30 pp | [+0.20 pp, +0.41 pp] |
| raw | within 5 min | 87.62% | 86.28% | -1.34 pp | [-1.50 pp, -1.09 pp] |
| raw | MAE (min) | 2.5810 | 3.1022 | +0.5212 | [+0.4653, +0.5540] |
| displayed | within 1 min | 61.67% | 60.64% | -1.03 pp | [-1.46 pp, -0.50 pp] |
| displayed | within 3 min | 80.76% | 79.28% | -1.48 pp | [-1.71 pp, -1.18 pp] |
| displayed | within 5 min | 88.01% | 86.23% | -1.78 pp | [-1.97 pp, -1.46 pp] |
| displayed | MAE (min) | 2.8684 | 3.1239 | +0.2555 | [+0.2139, +0.2837] |

### By service date (original labels, displayed)

| Date | Rows | A within 5 | B within 5 | A within 1 | B within 1 | A MAE | B MAE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-09-20 | 260,399 | 91.07% | 90.17% | 69.86% | 70.20% | 2.345 | 2.485 |
| 2026-09-21 | 284,748 | 88.23% | 86.34% | 61.35% | 59.97% | 2.798 | 3.059 |
| 2026-09-22 | 357,004 | 88.08% | 86.05% | 60.44% | 58.65% | 2.925 | 3.211 |
| 2026-09-23 | 348,876 | 87.91% | 85.89% | 60.56% | 59.17% | 2.856 | 3.146 |
| 2026-09-24 | 343,087 | 87.49% | 85.49% | 59.98% | 58.68% | 3.018 | 3.311 |
| 2026-09-25 | 334,760 | 86.08% | 84.25% | 58.15% | 57.00% | 3.076 | 3.348 |
| 2026-09-26 | 281,332 | 87.92% | 86.42% | 63.62% | 63.55% | 2.939 | 3.147 |

### Only the arrivals whose label the correction changed (status 2), original labels

| Model | View | Rows | Within 1 min | Within 5 min | MAE (min) |
|---|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | raw | 205,645 | 46.25% | 83.45% | 3.482 |
| A live (20261004T213106Z) | displayed | 205,645 | 49.53% | 78.29% | 6.266 |
| B strong (20261014T091943Z) | raw | 205,645 | 28.26% | 57.11% | 8.959 |
| B strong (20261014T091943Z) | displayed | 205,645 | 27.66% | 56.56% | 9.190 |

### Only the arrivals whose label is the same in both label sets, original labels

| Model | View | Rows | Within 1 min | Within 5 min | MAE (min) |
|---|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | raw | 2,004,561 | 50.95% | 88.05% | 2.489 |
| A live (20261004T213106Z) | displayed | 2,004,561 | 62.92% | 89.01% | 2.520 |
| B strong (20261014T091943Z) | raw | 2,004,561 | 64.03% | 89.27% | 2.501 |
| B strong (20261014T091943Z) | displayed | 2,004,561 | 64.02% | 89.28% | 2.502 |

## Scores on corrected labels

| Model | View | Within 1 min | Within 3 min | Within 5 min | MAE (min) |
|---|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | raw | 48.03% | 76.34% | 85.08% | 3.0897 |
| A live (20261004T213106Z) | displayed | 60.50% | 79.51% | 86.78% | 2.8231 |
| B strong (20261014T091943Z) | raw | 63.49% | 82.12% | 88.91% | 2.5808 |
| B strong (20261014T091943Z) | displayed | 63.59% | 82.24% | 89.01% | 2.5590 |

Share of arrivals inside the p25–p75 band (ideal 50%): A 52.02%, B 71.41%.

### B − A, paired service-date bootstrap (corrected labels)

2,000 paired resamples of the 7 service dates (seed 4231), percentile 95% interval, as in `summarize_recency_training.paired_day_bootstrap`. Positive within-rate differences and negative MAE differences favour B.

| View | Metric | A | B | B − A | 95% interval |
|---|---|---:|---:|---:|---|
| raw | within 1 min | 48.03% | 63.49% | +15.47 pp | [+15.01 pp, +15.84 pp] |
| raw | within 3 min | 76.34% | 82.12% | +5.79 pp | [+5.15 pp, +6.14 pp] |
| raw | within 5 min | 85.08% | 88.91% | +3.83 pp | [+3.34 pp, +4.10 pp] |
| raw | MAE (min) | 3.0897 | 2.5808 | -0.5089 | [-0.5448, -0.4392] |
| displayed | within 1 min | 60.50% | 63.59% | +3.09 pp | [+2.90 pp, +3.28 pp] |
| displayed | within 3 min | 79.51% | 82.24% | +2.73 pp | [+2.42 pp, +2.93 pp] |
| displayed | within 5 min | 86.78% | 89.01% | +2.23 pp | [+1.94 pp, +2.39 pp] |
| displayed | MAE (min) | 2.8231 | 2.5590 | -0.2641 | [-0.2876, -0.2208] |

### By service date (corrected labels, displayed)

| Date | Rows | A within 5 | B within 5 | A within 1 | B within 1 | A MAE | B MAE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-09-20 | 260,399 | 89.94% | 91.25% | 69.06% | 72.02% | 2.246 | 2.116 |
| 2026-09-21 | 284,748 | 86.94% | 89.33% | 59.95% | 63.02% | 2.763 | 2.478 |
| 2026-09-22 | 357,004 | 86.74% | 89.03% | 59.10% | 61.74% | 2.901 | 2.633 |
| 2026-09-23 | 348,876 | 86.63% | 88.96% | 59.20% | 62.28% | 2.836 | 2.548 |
| 2026-09-24 | 343,087 | 86.15% | 88.55% | 58.63% | 62.03% | 3.009 | 2.710 |
| 2026-09-25 | 334,760 | 84.91% | 87.34% | 57.03% | 60.08% | 3.029 | 2.749 |
| 2026-09-26 | 281,332 | 86.94% | 89.23% | 62.92% | 66.41% | 2.833 | 2.562 |

### Only the arrivals whose label the correction changed (status 2), corrected labels

| Model | View | Rows | Within 1 min | Within 5 min | MAE (min) |
|---|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | raw | 205,645 | 19.56% | 56.13% | 8.949 |
| A live (20261004T213106Z) | displayed | 205,645 | 36.94% | 65.08% | 5.780 |
| B strong (20261014T091943Z) | raw | 205,645 | 58.27% | 85.37% | 3.355 |
| B strong (20261014T091943Z) | displayed | 205,645 | 59.34% | 86.41% | 3.119 |

### Only the arrivals whose label is the same in both label sets, corrected labels

| Model | View | Rows | Within 1 min | Within 5 min | MAE (min) |
|---|---|---:|---:|---:|---:|
| A live (20261004T213106Z) | raw | 2,004,561 | 50.95% | 88.05% | 2.489 |
| A live (20261004T213106Z) | displayed | 2,004,561 | 62.92% | 89.01% | 2.520 |
| B strong (20261014T091943Z) | raw | 2,004,561 | 64.03% | 89.27% | 2.501 |
| B strong (20261014T091943Z) | displayed | 2,004,561 | 64.02% | 89.28% | 2.502 |

## How the comparison was made

- Rows: the locked test cache `data/ml/full_year/prepared/final-2026-09-26-robust-no-doy-mc20-rd56-eval`, re-read from the full-history shards (`data/ml/arrival_full`) in the same order; stop IDs and days checked row by row.
- Encoding: every row was encoded separately for each model through the production path (`FullHistory.encode` + `arrival_inputs`, what `full_year.prepare` uses) with that model's own manifest vocabulary, input prep (robust centre/scale) and notice-rate record (`NoticeRates.from_record` over `notice_rate_daily`, fingerprint-checked, read-only database): A 321 dates 2025-11-03..2026-09-19, B 293 dates 2025-11-03..2026-08-22. A category outside a model's vocabulary goes to that model's unknown slot 0. Inference: onnxruntime (CPU) on each artifact's `model.onnx`; the point estimate is the p50 output for both.
- Check on A: its re-encoded inputs equal the test cache byte for byte, and its score reproduces final.json (differences: within 1 0.0e+00, within 5 4.5e-07, MAE 8.0e-09).
- Check on B: re-encoding 2026-09-19 (281,211 rows) equals B's own evaluation cache byte for byte, and B reproduces its manifest selection score on that cache (3,726,635 rows; within 5 difference 0.0e+00, MAE difference 0.0e+00).
- Share of test arrivals with an unknown category (A / B): station 0.00% / 0.02%, train 1.27% / 2.87%, line 0.03% / 0.07%, category 0.00% / 0.01%, operator 0.00% / 0.00%.
- Corrected labels: the `scripts/build_trip_schedule_corrections.py` policy (version 2), same per-date logic, applied to the 7 test dates; output in `data/ml/live_vs_strong_20261014/corrected/`. Status counts: unverified 997, verified, plan unchanged 1,995,404, corrected to the dated plan 205,645, no matching change record 8,160. Labels changed on 205,645 arrivals. One mechanical adaptation: the producer reads mirror folders d-1..d+1, which under the mirror's older layout held exactly calendar days d-1..d+1; from mid-September the mirror files a day's late/early hours in the neighbouring folder, so the producer's own coverage check would have marked 2026-09-20 incomplete (status 5, all labels retained). This run selects the partitions whose declared hours lie on calendar days d-1..d+1 from folders d-2..d+2, i.e. the same evidence window. Partitions already in `data/archive` were only read; the 9 missing ones (2026-09-26 13h to 2026-09-27 23h) were downloaded into `data/ml/live_vs_strong_20261014/archive/`.

## Caveats

- Seven service dates only. The bootstrap clusters by date, so it cannot reflect trip-level dependence or differences between weeks; with 7 clusters the intervals are rough.
- B's training window ends four weeks before A's (2026-08-22 vs 2026-09-19). Part of any gap is data recency, not label correction or recency weighting.
- Original labels favour A (the label definition it trained on); corrected labels favour B's definition.
- B's quantiles are whole minutes (discrete CDF head), A's are continuous. That alone explains most of the raw within-1-minute gap; the displayed view, which rounds both, is the fair comparison for what users see.
- Historical five-hour-cutoff replay with present-day timetable patterns, not a logged prospective forecast.
- This report changes nothing in production: CURRENT, the deployed database and other experiments are untouched.
