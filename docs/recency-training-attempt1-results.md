> **First attempt: recency interpretation invalid.** Date-local batch normalization cancels the intended temporal multipliers. Scores, subgroup analysis and browser checks are retained as diagnosed evidence; this report cannot establish whether the requested recency bias helps. See [the task tracker](delay-model-open-tasks.md) and [layout audit](../data/ml/recency_training_20261013/weight-layout-audit.json). A matched, corrected-weighting comparison is required.

# Recency training comparison on corrected September labels

This is a report-only rescore of completed model outputs. It uses the same corrected prepared evaluation rows for control and recency-weighted fits, and does not load models or run inference.

**Service dates:** 2026-09-06 through 2026-09-19. **Eligible stop-arrivals:** 4,241,218. **Verification:** complete_with_documented_gaps

These are reused exploratory validation dates, not a fresh independent test. The input features retain present-day timetable patterns; historical timetable-as-of parity is unverified. Historical notice folds can use later records from other training months for earlier rows; this retrospective notice-fold limitation is unchanged.

The comparison has six views: fine-tuned median, shared classifier ±3-minute window, and shared classifier discrete median, each under control and recency weighting. The window and discrete outputs share the same binned classifier fit within a condition, but select epochs separately using their own frozen criteria; their selected checkpoints may differ.

## Overall metrics

Within rates divide by every eligible arrival. MAE is over scored rows; the report also records any missing coverage.

| Condition | View | Raw/displayed | Rows | Within 1m | Within 3m | Within 5m | MAE min | Missing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| control | Fine median | raw | 4241218 | 51.52% | 78.89% | 87.50% | 2.617 | 0 |
| control | Fine median | displayed | 4241218 | 61.54% | 81.14% | 88.42% | 2.628 | 0 |
| weighted | Fine median | raw | 4241218 | 49.57% | 78.63% | 87.41% | 2.615 | 0 |
| weighted | Fine median | displayed | 4241218 | 61.59% | 81.18% | 88.45% | 2.627 | 0 |
| control | Distribution window ±3 | raw | 4241218 | 22.82% | 86.26% | 90.69% | 3.371 | 0 |
| control | Distribution window ±3 | displayed | 4241218 | 22.82% | 86.25% | 90.69% | 3.371 | 0 |
| weighted | Distribution window ±3 | raw | 4241218 | 22.81% | 86.25% | 90.69% | 3.370 | 0 |
| weighted | Distribution window ±3 | displayed | 4241218 | 22.81% | 86.24% | 90.69% | 3.370 | 0 |
| control | Discrete median | raw | 4241218 | 64.32% | 82.35% | 88.90% | 2.549 | 0 |
| control | Discrete median | displayed | 4241218 | 64.32% | 82.36% | 88.91% | 2.550 | 0 |
| weighted | Discrete median | raw | 4241218 | 64.32% | 82.34% | 88.91% | 2.550 | 0 |
| weighted | Discrete median | displayed | 4241218 | 64.32% | 82.36% | 88.92% | 2.551 | 0 |

## Verified and unresolved corrected-label subsets

Verified contains status 1/2; unresolved contains status 0/3/4/5. The corrected labels are all retained in the complete-population score.

The pilot threshold audit distinguishes genuinely nonnegative corrections from labels that remain between −5 and 0: September 15 has 2,763 targets now at or above 0 and 100 still in [−5, 0); August 21 has 1,679 at or above 0 and 74 still in [−5, 0). The correction progress counter named `corrected_negative_to_nonnegative` uses the −5-minute threshold; it is not a count of targets becoming nonnegative.

Status codes: 0 unverified retained original; 1 source-verified original plan; 2 source-verified corrected dated plan; 3 ambiguous plan retained original; 4 unique plan without a matching post-event change timestamp retained original; 5 incomplete archive coverage retained original.

Status counts below cover this report's September 6–19 cohort.
| Correction status | Rows |
| --- | --- |
| 0 | 2794 |
| 1 | 3828248 |
| 2 | 392497 |
| 3 | 0 |
| 4 | 17679 |
| 5 | 0 |

| Condition | View | Label subset | Raw/displayed | Rows | Within 5m | MAE min |
| --- | --- | --- | --- | --- | --- | --- |
| control | fine | verified_1_2 | raw | 4220745 | 87.53% | 2.610 |
| control | fine | verified_1_2 | displayed | 4220745 | 88.45% | 2.620 |
| control | fine | unresolved_0_3_4_5 | raw | 20473 | 81.04% | 4.168 |
| control | fine | unresolved_0_3_4_5 | displayed | 20473 | 82.02% | 4.229 |
| weighted | fine | verified_1_2 | raw | 4220745 | 87.44% | 2.607 |
| weighted | fine | verified_1_2 | displayed | 4220745 | 88.49% | 2.619 |
| weighted | fine | unresolved_0_3_4_5 | raw | 20473 | 80.82% | 4.175 |
| weighted | fine | unresolved_0_3_4_5 | displayed | 20473 | 81.95% | 4.232 |
| control | window3 | verified_1_2 | raw | 4220745 | 90.72% | 3.363 |
| control | window3 | verified_1_2 | displayed | 4220745 | 90.72% | 3.363 |
| control | window3 | unresolved_0_3_4_5 | raw | 20473 | 84.02% | 4.925 |
| control | window3 | unresolved_0_3_4_5 | displayed | 20473 | 84.01% | 4.925 |
| weighted | window3 | verified_1_2 | raw | 4220745 | 90.73% | 3.363 |
| weighted | window3 | verified_1_2 | displayed | 4220745 | 90.73% | 3.363 |
| weighted | window3 | unresolved_0_3_4_5 | raw | 20473 | 84.03% | 4.924 |
| weighted | window3 | unresolved_0_3_4_5 | displayed | 20473 | 84.02% | 4.925 |
| control | discrete | verified_1_2 | raw | 4220745 | 88.94% | 2.540 |
| control | discrete | verified_1_2 | displayed | 4220745 | 88.94% | 2.542 |
| control | discrete | unresolved_0_3_4_5 | raw | 20473 | 82.18% | 4.259 |
| control | discrete | unresolved_0_3_4_5 | displayed | 20473 | 82.19% | 4.261 |
| weighted | discrete | verified_1_2 | raw | 4220745 | 88.94% | 2.541 |
| weighted | discrete | verified_1_2 | displayed | 4220745 | 88.95% | 2.542 |
| weighted | discrete | unresolved_0_3_4_5 | raw | 20473 | 82.23% | 4.260 |
| weighted | discrete | unresolved_0_3_4_5 | displayed | 20473 | 82.24% | 4.262 |

## Paired service-date bootstrap

Intervals use 2,000 paired resamples of the 14 service dates (seed 4231). Positive within-rate differences favor weighting; negative MAE differences favor weighting.

| View | Raw/displayed | Metric | Weighted − control | 95% paired day interval |
| --- | --- | --- | --- | --- |
| fine | raw | within_1_min | -0.01946 | [-0.02207, -0.01719] |
| fine | raw | within_3_min | -0.00265 | [-0.00351, -0.00190] |
| fine | raw | within_5_min | -0.00094 | [-0.00141, -0.00037] |
| fine | raw | mae_scored | -0.00234 | [-0.00818, +0.00284] |
| fine | displayed | within_1_min | +0.00047 | [-0.00027, +0.00122] |
| fine | displayed | within_3_min | +0.00034 | [-0.00008, +0.00079] |
| fine | displayed | within_5_min | +0.00033 | [-0.00009, +0.00102] |
| fine | displayed | mae_scored | -0.00133 | [-0.00500, +0.00205] |
| window3 | raw | within_1_min | -0.00011 | [-0.00023, +0.00001] |
| window3 | raw | within_3_min | -0.00012 | [-0.00023, -0.00002] |
| window3 | raw | within_5_min | +0.00005 | [+0.00001, +0.00009] |
| window3 | raw | mae_scored | -0.00032 | [-0.00080, +0.00010] |
| window3 | displayed | within_1_min | -0.00011 | [-0.00024, +0.00002] |
| window3 | displayed | within_3_min | -0.00012 | [-0.00023, -0.00001] |
| window3 | displayed | within_5_min | +0.00005 | [+0.00001, +0.00009] |
| window3 | displayed | mae_scored | -0.00034 | [-0.00082, +0.00008] |
| discrete | raw | within_1_min | +0.00002 | [-0.00013, +0.00017] |
| discrete | raw | within_3_min | -0.00005 | [-0.00015, +0.00003] |
| discrete | raw | within_5_min | +0.00005 | [-0.00005, +0.00014] |
| discrete | raw | mae_scored | +0.00091 | [+0.00040, +0.00144] |
| discrete | displayed | within_1_min | +0.00002 | [-0.00011, +0.00017] |
| discrete | displayed | within_3_min | -0.00004 | [-0.00014, +0.00004] |
| discrete | displayed | within_5_min | +0.00005 | [-0.00005, +0.00014] |
| discrete | displayed | mae_scored | +0.00091 | [+0.00041, +0.00143] |

## Group effects

The result JSON contains complete, verified, and unresolved metrics by service date, prepared service type, source, state, and route ID, plus distinct dated-trip counts per service date. Trip-stop IDs were looked up in bounded batches against the read-only database and checked against the prepared service date. Service labels use the frozen prepared segment dictionary; 0 rows differ from the current database train.segment value and are counted in the JSON. A disk-backed preflight found 1 route labels and estimates 1,363,968 bytes for per-group scores; it refuses estimates above 1 GiB. It also reports equal-weight paired effects by dated trip; the confidence interval resamples days, not trips. Route IDs are nullable; 4,241,218 rows ({"tts": 4241218}) lack a route ID and remain in UNKNOWN, so route results exclude attribution for those stops.

### Displayed metrics by date and service type

| Group | Date/service | Condition | View | Rows | Within 5m | MAE min |
| --- | --- | --- | --- | --- | --- | --- |
| service_date | 2026-09-06 | control | fine | 254765 | 91.92% | 2.009 |
| service_date | 2026-09-07 | control | fine | 307081 | 89.17% | 2.532 |
| service_date | 2026-09-08 | control | fine | 319648 | 89.34% | 2.577 |
| service_date | 2026-09-09 | control | fine | 308324 | 88.00% | 2.692 |
| service_date | 2026-09-10 | control | fine | 309527 | 88.94% | 2.529 |
| service_date | 2026-09-11 | control | fine | 304828 | 88.26% | 2.686 |
| service_date | 2026-09-12 | control | fine | 272970 | 91.23% | 2.160 |
| service_date | 2026-09-13 | control | fine | 251429 | 90.93% | 2.049 |
| service_date | 2026-09-14 | control | fine | 285527 | 87.50% | 2.761 |
| service_date | 2026-09-15 | control | fine | 337845 | 84.90% | 3.268 |
| service_date | 2026-09-16 | control | fine | 341873 | 86.35% | 3.071 |
| service_date | 2026-09-17 | control | fine | 336852 | 86.66% | 2.905 |
| service_date | 2026-09-18 | control | fine | 329338 | 86.83% | 2.842 |
| service_date | 2026-09-19 | control | fine | 281211 | 90.16% | 2.276 |
| service_date | 2026-09-06 | control | window3 | 254765 | 94.39% | 2.946 |
| service_date | 2026-09-07 | control | window3 | 307081 | 91.35% | 3.227 |
| service_date | 2026-09-08 | control | window3 | 319648 | 91.46% | 3.308 |
| service_date | 2026-09-09 | control | window3 | 308324 | 90.27% | 3.437 |
| service_date | 2026-09-10 | control | window3 | 309527 | 90.98% | 3.271 |
| service_date | 2026-09-11 | control | window3 | 304828 | 90.10% | 3.413 |
| service_date | 2026-09-12 | control | window3 | 272970 | 93.45% | 3.047 |
| service_date | 2026-09-13 | control | window3 | 251429 | 94.08% | 2.963 |
| service_date | 2026-09-14 | control | window3 | 285527 | 89.82% | 3.440 |
| service_date | 2026-09-15 | control | window3 | 337845 | 87.32% | 3.905 |
| service_date | 2026-09-16 | control | window3 | 341873 | 88.58% | 3.730 |
| service_date | 2026-09-17 | control | window3 | 336852 | 88.68% | 3.578 |
| service_date | 2026-09-18 | control | window3 | 329338 | 89.09% | 3.516 |
| service_date | 2026-09-19 | control | window3 | 281211 | 92.57% | 3.086 |
| service_date | 2026-09-06 | control | discrete | 254765 | 92.72% | 1.858 |
| service_date | 2026-09-07 | control | discrete | 307081 | 89.67% | 2.437 |
| service_date | 2026-09-08 | control | discrete | 319648 | 89.73% | 2.511 |
| service_date | 2026-09-09 | control | discrete | 308324 | 88.32% | 2.627 |
| service_date | 2026-09-10 | control | discrete | 309527 | 89.25% | 2.463 |
| service_date | 2026-09-11 | control | discrete | 304828 | 88.54% | 2.630 |
| service_date | 2026-09-12 | control | discrete | 272970 | 91.76% | 2.080 |
| service_date | 2026-09-13 | control | discrete | 251429 | 92.30% | 1.876 |
| service_date | 2026-09-14 | control | discrete | 285527 | 88.00% | 2.649 |
| service_date | 2026-09-15 | control | discrete | 337845 | 85.21% | 3.220 |
| service_date | 2026-09-16 | control | discrete | 341873 | 86.73% | 3.015 |
| service_date | 2026-09-17 | control | discrete | 336852 | 87.01% | 2.861 |
| service_date | 2026-09-18 | control | discrete | 329338 | 87.21% | 2.810 |
| service_date | 2026-09-19 | control | discrete | 281211 | 90.88% | 2.178 |
| service_date | 2026-09-06 | weighted | fine | 254765 | 92.02% | 2.008 |
| service_date | 2026-09-07 | weighted | fine | 307081 | 89.20% | 2.526 |
| service_date | 2026-09-08 | weighted | fine | 319648 | 89.36% | 2.569 |
| service_date | 2026-09-09 | weighted | fine | 308324 | 87.99% | 2.690 |
| service_date | 2026-09-10 | weighted | fine | 309527 | 88.92% | 2.531 |
| service_date | 2026-09-11 | weighted | fine | 304828 | 88.24% | 2.690 |
| service_date | 2026-09-12 | weighted | fine | 272970 | 91.19% | 2.170 |
| service_date | 2026-09-13 | weighted | fine | 251429 | 91.43% | 2.030 |
| service_date | 2026-09-14 | weighted | fine | 285527 | 87.49% | 2.760 |
| service_date | 2026-09-15 | weighted | fine | 337845 | 84.87% | 3.265 |
| service_date | 2026-09-16 | weighted | fine | 341873 | 86.40% | 3.064 |
| service_date | 2026-09-17 | weighted | fine | 336852 | 86.65% | 2.907 |
| service_date | 2026-09-18 | weighted | fine | 329338 | 86.84% | 2.845 |
| service_date | 2026-09-19 | weighted | fine | 281211 | 90.13% | 2.286 |
| service_date | 2026-09-06 | weighted | window3 | 254765 | 94.39% | 2.946 |
| service_date | 2026-09-07 | weighted | window3 | 307081 | 91.35% | 3.227 |
| service_date | 2026-09-08 | weighted | window3 | 319648 | 91.46% | 3.309 |
| service_date | 2026-09-09 | weighted | window3 | 308324 | 90.27% | 3.436 |
| service_date | 2026-09-10 | weighted | window3 | 309527 | 90.99% | 3.271 |
| service_date | 2026-09-11 | weighted | window3 | 304828 | 90.10% | 3.413 |
| service_date | 2026-09-12 | weighted | window3 | 272970 | 93.46% | 3.047 |
| service_date | 2026-09-13 | weighted | window3 | 251429 | 94.08% | 2.962 |
| service_date | 2026-09-14 | weighted | window3 | 285527 | 89.81% | 3.439 |
| service_date | 2026-09-15 | weighted | window3 | 337845 | 87.32% | 3.903 |
| service_date | 2026-09-16 | weighted | window3 | 341873 | 88.59% | 3.730 |
| service_date | 2026-09-17 | weighted | window3 | 336852 | 88.70% | 3.577 |
| service_date | 2026-09-18 | weighted | window3 | 329338 | 89.10% | 3.514 |
| service_date | 2026-09-19 | weighted | window3 | 281211 | 92.57% | 3.086 |
| service_date | 2026-09-06 | weighted | discrete | 254765 | 92.71% | 1.858 |
| service_date | 2026-09-07 | weighted | discrete | 307081 | 89.68% | 2.438 |
| service_date | 2026-09-08 | weighted | discrete | 319648 | 89.74% | 2.512 |
| service_date | 2026-09-09 | weighted | discrete | 308324 | 88.34% | 2.627 |
| service_date | 2026-09-10 | weighted | discrete | 309527 | 89.29% | 2.464 |
| service_date | 2026-09-11 | weighted | discrete | 304828 | 88.55% | 2.632 |
| service_date | 2026-09-12 | weighted | discrete | 272970 | 91.77% | 2.080 |
| service_date | 2026-09-13 | weighted | discrete | 251429 | 92.29% | 1.876 |
| service_date | 2026-09-14 | weighted | discrete | 285527 | 87.99% | 2.651 |
| service_date | 2026-09-15 | weighted | discrete | 337845 | 85.23% | 3.219 |
| service_date | 2026-09-16 | weighted | discrete | 341873 | 86.71% | 3.018 |
| service_date | 2026-09-17 | weighted | discrete | 336852 | 87.04% | 2.862 |
| service_date | 2026-09-18 | weighted | discrete | 329338 | 87.20% | 2.811 |
| service_date | 2026-09-19 | weighted | discrete | 281211 | 90.86% | 2.179 |
| service | Long-distance | control | fine | 69731 | 50.80% | 10.850 |
| service | Regional | control | fine | 2046223 | 85.62% | 3.087 |
| service | S-Bahn | control | fine | 2125264 | 92.35% | 1.916 |
| service | Long-distance | control | window3 | 69731 | 53.26% | 12.592 |
| service | Regional | control | window3 | 2046223 | 88.34% | 3.689 |
| service | S-Bahn | control | window3 | 2125264 | 94.18% | 2.762 |
| service | Long-distance | control | discrete | 69731 | 52.71% | 11.233 |
| service | Regional | control | discrete | 2046223 | 86.16% | 3.014 |
| service | S-Bahn | control | discrete | 2125264 | 92.74% | 1.818 |
| service | Long-distance | weighted | fine | 69731 | 50.73% | 10.836 |
| service | Regional | weighted | fine | 2046223 | 85.62% | 3.087 |
| service | S-Bahn | weighted | fine | 2125264 | 92.42% | 1.914 |
| service | Long-distance | weighted | window3 | 69731 | 53.24% | 12.593 |
| service | Regional | weighted | window3 | 2046223 | 88.34% | 3.688 |
| service | S-Bahn | weighted | window3 | 2125264 | 94.19% | 2.762 |
| service | Long-distance | weighted | discrete | 69731 | 52.74% | 11.263 |
| service | Regional | weighted | discrete | 2046223 | 86.16% | 3.014 |
| service | S-Bahn | weighted | discrete | 2125264 | 92.76% | 1.819 |

## Export, browser, and resources

Every candidate is gated on successful ONNX export parity and a passed actual Chromium browser check. Artifact size includes the full manifest and model file; the strict target is 35,000,000 bytes.

| Condition | Fit/view | Manifest-inclusive bytes | ≤35 MB | Browser pass | Cgroup peak bytes |
| --- | --- | --- | --- | --- | --- |
| control | fine | 15859344 | True | True | 5369196544 |
| control | distribution | 16161131 | True | True | 3749142528 |
| control | discrete | 16162081 | True | True | 1618165760 |
| weighted | fine | 15859341 | True | True | 3598368768 |
| weighted | distribution | 16161128 | True | True | 3724955648 |
| weighted | discrete | 16162075 | True | True | 1616990208 |

## Full later-window quantile diagnostics

These are the frozen producers’ full later-window diagnostics, not values reconstructed from point arrays. Producer scoring validates finite ordered percentile triplets; each quantile denominator must equal the eligible cohort. Discrete metrics come from full-cohort ONNX CPU replay. Point-only saved arrays cannot independently reproduce interval coverage.

| Condition | View | Quantile-scored | P25–P75 coverage | At/below P25 | At/below P75 |
| --- | --- | --- | --- | --- | --- |
| control | fine | 4241218 | 57.65% | 18.26% | 75.92% |
| control | distribution | 4241218 | 29.46% | 48.38% | 77.84% |
| control | discrete | 4241218 | 69.85% | 48.38% | 77.84% |
| weighted | fine | 4241218 | 57.08% | 17.64% | 74.72% |
| weighted | distribution | 4241218 | 29.43% | 48.41% | 77.85% |
| weighted | discrete | 4241218 | 69.79% | 48.41% | 77.85% |

## Paired dated-trip effects

Each dated trip is one cluster; the table reports equal-cluster means over the corrected September cohort. Within-five gains are positive; MAE reductions are negative.

| View | Metric | Control | Weighted | Weighted − control | Clusters improved |
| --- | --- | --- | --- | --- | --- |
| fine | within_5_min_displayed | 0.87438 | 0.87453 | +0.00015 | 0.9% |
| fine | mae_displayed | 2.84221 | 2.84204 | -0.00017 | 14.9% |
| window3 | within_5_min_displayed | 0.89768 | 0.89775 | +0.00008 | 0.5% |
| window3 | mae_displayed | 3.58379 | 3.58263 | -0.00116 | 12.0% |
| discrete | within_5_min_displayed | 0.87944 | 0.87930 | -0.00014 | 1.3% |
| discrete | mae_displayed | 2.77362 | 2.77513 | +0.00151 | 20.1% |

## Correction-only baseline effect

The unchanged-checkpoint correction-only rescore is a separate analysis from refitting and recency weighting. Its old-target and corrected-target metrics and their difference are recorded in the JSON for the same September 6–19 cohort; source-verified and unresolved target subsets remain separate. This report does not attribute target-correction gains to recency.

The correction baseline is independently keyed to the completed correction result, prepared cache, status file, original exports, and prediction array hashes. The cohort-only corrected-minus-original differences are:

| Unchanged output | Raw/displayed | Δ within 5m | Δ MAE min |
| --- | --- | --- | --- |
| reference | raw | -0.02642 | +0.533 |
| reference | displayed | -0.01271 | -0.055 |
| fine | raw | -0.02687 | +0.542 |
| fine | displayed | -0.01292 | -0.051 |
| distribution | raw | -0.02377 | +0.262 |
| distribution | displayed | -0.01354 | -0.029 |
| discrete | raw | -0.02355 | +0.287 |
| discrete | displayed | -0.01255 | -0.048 |

## August comparison with verified correction overlay

The corrected August comparison preserves the frozen August 1–31 matched population and overlays corrected labels only where source status is verified. Uncovered rows, including August 22, remain explicitly unverified original labels. This is descriptive, not a new forecast test.

Supported recurring trips: 24,252; mean September 15 minus August mean: +0.63 minutes; median: -0.40 minutes.
| Supported-trip deviation | Share |
| --- | --- |
| >5 min | 10.71% |
| >15 min | 2.99% |
| >30 min | 0.76% |

The full 45-date coverage and per-date verification counts are bound in the result JSON.

## Interpretation limits

A better reused validation score is not evidence of future generalization. The date bootstrap clusters only by service date, not recurring trip. Historical notice folds retain their cross-month look-ahead; timetable inputs are not historically reconstructed. The correction changes retrospective targets only and leaves the original forecast-time feature encoding intact. No result here changes the production pointer or authorizes promotion.

Input status: complete_with_documented_gaps. Resource records are copied from the completed run manifests; this summary process did not measure model inference or browser latency.
