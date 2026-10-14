# Recency training comparison on corrected September labels

## In plain words

> Followed by [the v3 comparison](recency-training-v3-results.md), which tests 3× weighting and time of year.


**Giving the last month more weight made no practical difference.** Weighted and control models
are within about 0.1 percentage point of each other at the 5-minute mark in every view, on the
same 4.24 million September arrivals:

| View (app-displayed) | Within 1 min | Within 3 min | Within 5 min | Avg error |
| --- | --- | --- | --- | --- |
| Fine median, control → weighted | 61.21% → 61.34% | 80.78% → 80.86% | 88.15% → 88.27% | 2.650 → 2.637 min |
| ±3-min window, control → weighted | 23.74% → 23.76% | 86.14% → 86.13% | 90.53% → 90.54% | 3.357 → 3.359 min |
| Discrete median, control → weighted | 63.90% → 63.98% | 82.13% → 82.14% | 88.72% → 88.74% | 2.572 → 2.568 min |

Some of these tiny gains are statistically consistent (for example, +0.12 points within 5 min
for the fine model). They are far too small for a rider to notice. The raw fine model is slightly
*worse* within 1 minute (−0.67 points). The weighting tested was mild: the last month counted up
to 1.5×, and older data counted 1×.

**What did help is the label correction plus retraining.** Against corrected labels, the old
live model scores 84.74% within 5 min (raw). The retrained models score 87.2% (fine), 88.6%
(discrete) and 90.4% (±3-min window), with or without weighting. All six models pass the export,
browser and size checks. `CURRENT` is unchanged; replacing it is the user's decision.

This is a report-only rescore of completed model outputs. It uses the same corrected prepared evaluation rows for control and recency-weighted fits, and does not load models or run inference.

**Service dates:** 2026-09-06 through 2026-09-19. **Eligible stop-arrivals:** 4,241,218. **Verification:** complete_with_documented_gaps

These are reused exploratory validation dates, not a fresh independent test. The input features retain present-day timetable patterns; historical timetable-as-of parity is unverified. Historical notice folds can use later records from other training months for earlier rows; this retrospective notice-fold limitation is unchanged.

The comparison has six views: fine-tuned median, shared classifier ±3-minute window, and shared classifier discrete median, each under control and recency weighting. The window and discrete outputs share the same binned classifier fit within a condition, but select epochs separately using their own frozen criteria; their selected checkpoints may differ.

## Overall metrics

Within rates divide by every eligible arrival. MAE is over scored rows; the report also records any missing coverage.

| Condition | View | Raw/displayed | Rows | Within 1m | Within 3m | Within 5m | MAE min | Missing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| control | Fine median | raw | 4241218 | 51.61% | 78.59% | 87.19% | 2.640 | 0 |
| control | Fine median | displayed | 4241218 | 61.21% | 80.78% | 88.15% | 2.650 | 0 |
| weighted | Fine median | raw | 4241218 | 50.94% | 78.56% | 87.25% | 2.643 | 0 |
| weighted | Fine median | displayed | 4241218 | 61.34% | 80.86% | 88.27% | 2.637 | 0 |
| control | Distribution window ±3 | raw | 4241218 | 23.62% | 86.04% | 90.42% | 3.386 | 0 |
| control | Distribution window ±3 | displayed | 4241218 | 23.74% | 86.14% | 90.53% | 3.357 | 0 |
| weighted | Distribution window ±3 | raw | 4241218 | 23.64% | 86.03% | 90.42% | 3.391 | 0 |
| weighted | Distribution window ±3 | displayed | 4241218 | 23.76% | 86.13% | 90.54% | 3.359 | 0 |
| control | Discrete median | raw | 4241218 | 63.81% | 82.03% | 88.62% | 2.594 | 0 |
| control | Discrete median | displayed | 4241218 | 63.90% | 82.13% | 88.72% | 2.572 | 0 |
| weighted | Discrete median | raw | 4241218 | 63.89% | 82.04% | 88.64% | 2.590 | 0 |
| weighted | Discrete median | displayed | 4241218 | 63.98% | 82.14% | 88.74% | 2.568 | 0 |

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
| control | fine | verified_1_2 | raw | 4220745 | 87.22% | 2.633 |
| control | fine | verified_1_2 | displayed | 4220745 | 88.18% | 2.642 |
| control | fine | unresolved_0_3_4_5 | raw | 20473 | 81.22% | 4.152 |
| control | fine | unresolved_0_3_4_5 | displayed | 20473 | 82.21% | 4.210 |
| weighted | fine | verified_1_2 | raw | 4220745 | 87.28% | 2.636 |
| weighted | fine | verified_1_2 | displayed | 4220745 | 88.30% | 2.630 |
| weighted | fine | unresolved_0_3_4_5 | raw | 20473 | 80.95% | 4.163 |
| weighted | fine | unresolved_0_3_4_5 | displayed | 20473 | 82.01% | 4.220 |
| control | window3 | verified_1_2 | raw | 4220745 | 90.45% | 3.379 |
| control | window3 | verified_1_2 | displayed | 4220745 | 90.56% | 3.350 |
| control | window3 | unresolved_0_3_4_5 | raw | 20473 | 84.19% | 4.856 |
| control | window3 | unresolved_0_3_4_5 | displayed | 20473 | 84.15% | 4.869 |
| weighted | window3 | verified_1_2 | raw | 4220745 | 90.45% | 3.384 |
| weighted | window3 | verified_1_2 | displayed | 4220745 | 90.57% | 3.352 |
| weighted | window3 | unresolved_0_3_4_5 | raw | 20473 | 84.19% | 4.859 |
| weighted | window3 | unresolved_0_3_4_5 | displayed | 20473 | 84.16% | 4.875 |
| control | discrete | verified_1_2 | raw | 4220745 | 88.65% | 2.586 |
| control | discrete | verified_1_2 | displayed | 4220745 | 88.75% | 2.564 |
| control | discrete | unresolved_0_3_4_5 | raw | 20473 | 82.40% | 4.190 |
| control | discrete | unresolved_0_3_4_5 | displayed | 20473 | 82.38% | 4.202 |
| weighted | discrete | verified_1_2 | raw | 4220745 | 88.67% | 2.583 |
| weighted | discrete | verified_1_2 | displayed | 4220745 | 88.77% | 2.560 |
| weighted | discrete | unresolved_0_3_4_5 | raw | 20473 | 82.45% | 4.181 |
| weighted | discrete | unresolved_0_3_4_5 | displayed | 20473 | 82.46% | 4.194 |

## Paired service-date bootstrap

Intervals use 2,000 paired resamples of the 14 service dates (seed 4231). Positive within-rate differences favor weighting; negative MAE differences favor weighting.

| View | Raw/displayed | Metric | Weighted − control | 95% paired day interval |
| --- | --- | --- | --- | --- |
| fine | raw | within_1_min | -0.00665 | [-0.00806, -0.00527] |
| fine | raw | within_3_min | -0.00032 | [-0.00071, +0.00012] |
| fine | raw | within_5_min | +0.00063 | [+0.00023, +0.00112] |
| fine | raw | mae_scored | +0.00278 | [-0.00720, +0.01641] |
| fine | displayed | within_1_min | +0.00127 | [+0.00039, +0.00242] |
| fine | displayed | within_3_min | +0.00082 | [+0.00041, +0.00125] |
| fine | displayed | within_5_min | +0.00118 | [+0.00064, +0.00180] |
| fine | displayed | mae_scored | -0.01222 | [-0.01660, -0.00841] |
| window3 | raw | within_1_min | +0.00019 | [-0.00015, +0.00051] |
| window3 | raw | within_3_min | -0.00007 | [-0.00042, +0.00023] |
| window3 | raw | within_5_min | -0.00003 | [-0.00035, +0.00026] |
| window3 | raw | mae_scored | +0.00449 | [-0.00258, +0.01144] |
| window3 | displayed | within_1_min | +0.00020 | [-0.00010, +0.00052] |
| window3 | displayed | within_3_min | -0.00004 | [-0.00027, +0.00018] |
| window3 | displayed | within_5_min | +0.00002 | [-0.00016, +0.00021] |
| window3 | displayed | mae_scored | +0.00229 | [-0.00036, +0.00474] |
| discrete | raw | within_1_min | +0.00081 | [-0.00002, +0.00177] |
| discrete | raw | within_3_min | +0.00007 | [-0.00032, +0.00049] |
| discrete | raw | within_5_min | +0.00021 | [-0.00016, +0.00053] |
| discrete | raw | mae_scored | -0.00320 | [-0.00946, +0.00257] |
| discrete | displayed | within_1_min | +0.00082 | [-0.00002, +0.00174] |
| discrete | displayed | within_3_min | +0.00009 | [-0.00026, +0.00047] |
| discrete | displayed | within_5_min | +0.00022 | [-0.00010, +0.00052] |
| discrete | displayed | mae_scored | -0.00379 | [-0.00680, -0.00103] |

## Group effects

The result JSON contains complete, verified, and unresolved metrics by service date, prepared service type, source, state, and route ID, plus distinct dated-trip counts per service date. Trip-stop IDs were looked up in bounded batches against the read-only database and checked against the prepared service date. Service labels use the frozen prepared segment dictionary; 0 rows differ from the current database train.segment value and are counted in the JSON. A disk-backed preflight found 1 route labels and estimates 1,363,968 bytes for per-group scores; it refuses estimates above 1 GiB. It also reports equal-weight paired effects by dated trip; the confidence interval resamples days, not trips. Route IDs are nullable; 4,241,218 rows ({"tts": 4241218}) lack a route ID and remain in UNKNOWN, so route results exclude attribution for those stops.

### Displayed metrics by date and service type

| Group | Date/service | Condition | View | Rows | Within 5m | MAE min |
| --- | --- | --- | --- | --- | --- | --- |
| service_date | 2026-09-06 | control | fine | 254765 | 91.48% | 2.048 |
| service_date | 2026-09-07 | control | fine | 307081 | 89.09% | 2.533 |
| service_date | 2026-09-08 | control | fine | 319648 | 89.19% | 2.585 |
| service_date | 2026-09-09 | control | fine | 308324 | 87.97% | 2.699 |
| service_date | 2026-09-10 | control | fine | 309527 | 88.66% | 2.553 |
| service_date | 2026-09-11 | control | fine | 304828 | 88.12% | 2.700 |
| service_date | 2026-09-12 | control | fine | 272970 | 89.98% | 2.256 |
| service_date | 2026-09-13 | control | fine | 251429 | 90.35% | 2.109 |
| service_date | 2026-09-14 | control | fine | 285527 | 87.48% | 2.753 |
| service_date | 2026-09-15 | control | fine | 337845 | 84.85% | 3.270 |
| service_date | 2026-09-16 | control | fine | 341873 | 86.46% | 3.057 |
| service_date | 2026-09-17 | control | fine | 336852 | 86.55% | 2.913 |
| service_date | 2026-09-18 | control | fine | 329338 | 86.82% | 2.845 |
| service_date | 2026-09-19 | control | fine | 281211 | 88.99% | 2.367 |
| service_date | 2026-09-06 | control | window3 | 254765 | 93.95% | 2.959 |
| service_date | 2026-09-07 | control | window3 | 307081 | 91.27% | 3.210 |
| service_date | 2026-09-08 | control | window3 | 319648 | 91.36% | 3.296 |
| service_date | 2026-09-09 | control | window3 | 308324 | 90.27% | 3.419 |
| service_date | 2026-09-10 | control | window3 | 309527 | 90.94% | 3.239 |
| service_date | 2026-09-11 | control | window3 | 304828 | 90.11% | 3.388 |
| service_date | 2026-09-12 | control | window3 | 272970 | 93.31% | 2.995 |
| service_date | 2026-09-13 | control | window3 | 251429 | 92.98% | 3.021 |
| service_date | 2026-09-14 | control | window3 | 285527 | 89.68% | 3.451 |
| service_date | 2026-09-15 | control | window3 | 337845 | 87.27% | 3.887 |
| service_date | 2026-09-16 | control | window3 | 341873 | 88.58% | 3.712 |
| service_date | 2026-09-17 | control | window3 | 336852 | 88.67% | 3.544 |
| service_date | 2026-09-18 | control | window3 | 329338 | 89.03% | 3.490 |
| service_date | 2026-09-19 | control | window3 | 281211 | 92.26% | 3.082 |
| service_date | 2026-09-06 | control | discrete | 254765 | 92.29% | 1.953 |
| service_date | 2026-09-07 | control | discrete | 307081 | 89.51% | 2.462 |
| service_date | 2026-09-08 | control | discrete | 319648 | 89.60% | 2.527 |
| service_date | 2026-09-09 | control | discrete | 308324 | 88.35% | 2.632 |
| service_date | 2026-09-10 | control | discrete | 309527 | 89.18% | 2.463 |
| service_date | 2026-09-11 | control | discrete | 304828 | 88.43% | 2.634 |
| service_date | 2026-09-12 | control | discrete | 272970 | 91.49% | 2.098 |
| service_date | 2026-09-13 | control | discrete | 251429 | 91.18% | 2.022 |
| service_date | 2026-09-14 | control | discrete | 285527 | 87.92% | 2.683 |
| service_date | 2026-09-15 | control | discrete | 337845 | 85.18% | 3.213 |
| service_date | 2026-09-16 | control | discrete | 341873 | 86.80% | 3.006 |
| service_date | 2026-09-17 | control | discrete | 336852 | 86.89% | 2.855 |
| service_date | 2026-09-18 | control | discrete | 329338 | 87.08% | 2.795 |
| service_date | 2026-09-19 | control | discrete | 281211 | 90.46% | 2.231 |
| service_date | 2026-09-06 | weighted | fine | 254765 | 91.67% | 2.033 |
| service_date | 2026-09-07 | weighted | fine | 307081 | 89.17% | 2.522 |
| service_date | 2026-09-08 | weighted | fine | 319648 | 89.26% | 2.575 |
| service_date | 2026-09-09 | weighted | fine | 308324 | 88.05% | 2.683 |
| service_date | 2026-09-10 | weighted | fine | 309527 | 88.69% | 2.550 |
| service_date | 2026-09-11 | weighted | fine | 304828 | 88.15% | 2.695 |
| service_date | 2026-09-12 | weighted | fine | 272970 | 90.36% | 2.228 |
| service_date | 2026-09-13 | weighted | fine | 251429 | 90.65% | 2.078 |
| service_date | 2026-09-14 | weighted | fine | 285527 | 87.55% | 2.744 |
| service_date | 2026-09-15 | weighted | fine | 337845 | 84.91% | 3.259 |
| service_date | 2026-09-16 | weighted | fine | 341873 | 86.51% | 3.047 |
| service_date | 2026-09-17 | weighted | fine | 336852 | 86.59% | 2.906 |
| service_date | 2026-09-18 | weighted | fine | 329338 | 86.86% | 2.844 |
| service_date | 2026-09-19 | weighted | fine | 281211 | 89.33% | 2.346 |
| service_date | 2026-09-06 | weighted | window3 | 254765 | 93.95% | 2.958 |
| service_date | 2026-09-07 | weighted | window3 | 307081 | 91.30% | 3.205 |
| service_date | 2026-09-08 | weighted | window3 | 319648 | 91.36% | 3.298 |
| service_date | 2026-09-09 | weighted | window3 | 308324 | 90.24% | 3.425 |
| service_date | 2026-09-10 | weighted | window3 | 309527 | 90.94% | 3.247 |
| service_date | 2026-09-11 | weighted | window3 | 304828 | 90.10% | 3.394 |
| service_date | 2026-09-12 | weighted | window3 | 272970 | 93.29% | 3.005 |
| service_date | 2026-09-13 | weighted | window3 | 251429 | 93.10% | 3.018 |
| service_date | 2026-09-14 | weighted | window3 | 285527 | 89.73% | 3.443 |
| service_date | 2026-09-15 | weighted | window3 | 337845 | 87.28% | 3.887 |
| service_date | 2026-09-16 | weighted | window3 | 341873 | 88.58% | 3.716 |
| service_date | 2026-09-17 | weighted | window3 | 336852 | 88.66% | 3.550 |
| service_date | 2026-09-18 | weighted | window3 | 329338 | 89.02% | 3.494 |
| service_date | 2026-09-19 | weighted | window3 | 281211 | 92.19% | 3.085 |
| service_date | 2026-09-06 | weighted | discrete | 254765 | 92.28% | 1.946 |
| service_date | 2026-09-07 | weighted | discrete | 307081 | 89.55% | 2.447 |
| service_date | 2026-09-08 | weighted | discrete | 319648 | 89.56% | 2.526 |
| service_date | 2026-09-09 | weighted | discrete | 308324 | 88.28% | 2.636 |
| service_date | 2026-09-10 | weighted | discrete | 309527 | 89.23% | 2.459 |
| service_date | 2026-09-11 | weighted | discrete | 304828 | 88.45% | 2.636 |
| service_date | 2026-09-12 | weighted | discrete | 272970 | 91.54% | 2.095 |
| service_date | 2026-09-13 | weighted | discrete | 251429 | 91.33% | 2.015 |
| service_date | 2026-09-14 | weighted | discrete | 285527 | 87.95% | 2.670 |
| service_date | 2026-09-15 | weighted | discrete | 337845 | 85.22% | 3.209 |
| service_date | 2026-09-16 | weighted | discrete | 341873 | 86.78% | 3.007 |
| service_date | 2026-09-17 | weighted | discrete | 336852 | 86.98% | 2.853 |
| service_date | 2026-09-18 | weighted | discrete | 329338 | 87.14% | 2.788 |
| service_date | 2026-09-19 | weighted | discrete | 281211 | 90.38% | 2.233 |
| service | Long-distance | control | fine | 69731 | 50.19% | 10.796 |
| service | Regional | control | fine | 2046223 | 85.56% | 3.097 |
| service | S-Bahn | control | fine | 2125264 | 91.89% | 1.952 |
| service | Long-distance | control | window3 | 69731 | 53.66% | 12.242 |
| service | Regional | control | window3 | 2046223 | 88.25% | 3.677 |
| service | S-Bahn | control | window3 | 2125264 | 93.94% | 2.757 |
| service | Long-distance | control | discrete | 69731 | 51.02% | 10.846 |
| service | Regional | control | discrete | 2046223 | 86.04% | 3.039 |
| service | S-Bahn | control | discrete | 2125264 | 92.54% | 1.850 |
| service | Long-distance | weighted | fine | 69731 | 50.24% | 10.775 |
| service | Regional | weighted | fine | 2046223 | 85.60% | 3.092 |
| service | S-Bahn | weighted | fine | 2125264 | 92.08% | 1.933 |
| service | Long-distance | weighted | window3 | 69731 | 53.68% | 12.294 |
| service | Regional | weighted | window3 | 2046223 | 88.25% | 3.677 |
| service | S-Bahn | weighted | window3 | 2125264 | 93.95% | 2.760 |
| service | Long-distance | weighted | discrete | 69731 | 51.38% | 10.852 |
| service | Regional | weighted | discrete | 2046223 | 86.06% | 3.033 |
| service | S-Bahn | weighted | discrete | 2125264 | 92.55% | 1.848 |

## Export, browser, and resources

Every candidate is gated on successful ONNX export parity and a passed actual Chromium browser check. Artifact size includes the full manifest and model file; the strict target is 35,000,000 bytes.

| Condition | Fit/view | Manifest-inclusive bytes | ≤35 MB | Browser pass | Cgroup peak bytes |
| --- | --- | --- | --- | --- | --- |
| control | fine | 15859355 | True | True | 4627525632 |
| control | distribution | 16161157 | True | True | 3966877696 |
| control | discrete | 16162110 | True | True | 1618853888 |
| weighted | fine | 15859369 | True | True | 4948799488 |
| weighted | distribution | 16161155 | True | True | 1383309312 |
| weighted | discrete | 16162115 | True | True | 1618137088 |

## Full later-window quantile diagnostics

These are the frozen producers’ full later-window diagnostics, not values reconstructed from point arrays. Producer scoring validates finite ordered percentile triplets; each quantile denominator must equal the eligible cohort. Discrete metrics come from full-cohort ONNX CPU replay. Point-only saved arrays cannot independently reproduce interval coverage.

| Condition | View | Quantile-scored | P25–P75 coverage | At/below P25 | At/below P75 |
| --- | --- | --- | --- | --- | --- |
| control | fine | 4241218 | 51.18% | 25.59% | 76.77% |
| control | distribution | 4241218 | 31.24% | 47.82% | 79.06% |
| control | discrete | 4241218 | 71.25% | 47.82% | 79.06% |
| weighted | fine | 4241218 | 53.78% | 21.45% | 75.23% |
| weighted | distribution | 4241218 | 31.28% | 47.75% | 79.03% |
| weighted | discrete | 4241218 | 71.29% | 47.75% | 79.03% |

## Paired dated-trip effects

Each dated trip is one cluster; the table reports equal-cluster means over the corrected September cohort. Within-five gains are positive; MAE reductions are negative.

| View | Metric | Control | Weighted | Weighted − control | Clusters improved |
| --- | --- | --- | --- | --- | --- |
| fine | within_5_min_displayed | 0.87310 | 0.87362 | +0.00052 | 1.7% |
| fine | mae_displayed | 2.85662 | 2.85128 | -0.00534 | 21.5% |
| window3 | within_5_min_displayed | 0.89662 | 0.89664 | +0.00001 | 0.9% |
| window3 | mae_displayed | 3.56933 | 3.57232 | +0.00299 | 18.5% |
| discrete | within_5_min_displayed | 0.87756 | 0.87789 | +0.00032 | 2.4% |
| discrete | mae_displayed | 2.79693 | 2.79165 | -0.00529 | 26.6% |

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
