# Recency training comparison on corrected September labels

## In plain words

> **Decision (2026-10-14):** the user approved replacing `CURRENT` with the strong discrete model, pending a head-to-head on live outcomes. See `openspec/changes/replace-current-with-strong-discrete`.


**Strong recency weighting (last month up to 3×): a small, real improvement — still too small for a rider to notice.**
**Time of year (day and month, no year): worse in every view.**

App-displayed forecasts, on the same 4.24 million September arrivals:

| Model | Within 1 min | Within 3 min | Within 5 min | Avg error |
| --- | --- | --- | --- | --- |
| Fine: control / strong / seasonal | 61.2% / 61.6% / 60.5% | 80.8% / 81.1% / 79.4% | 88.2% / 88.4% / 87.1% | 2.65 / 2.62 / 2.74 min |
| ±3-min window: control / strong / seasonal | 23.7% / 23.8% / 22.2% | 86.1% / 86.2% / 84.0% | 90.5% / 90.6% / 90.0% | 3.36 / 3.35 / 3.40 min |
| Discrete: control / strong / seasonal | 63.9% / 64.1% / 60.1% | 82.1% / 82.2% / 80.7% | 88.7% / 88.8% / 88.0% | 2.57 / 2.56 / 2.77 min |

- **Strong:** better than control in all three views, and the paired per-day intervals exclude zero for most metrics. But the gains are 0.05–0.3 percentage points and 0.01–0.03 min of average error. One exception: the raw (unrounded) fine model is 2.6 points *worse* within 1 minute. The app's rounding hides that, but it shows that heavier weighting makes the raw fine forecasts less sharp.
- **Seasonal:** worse everywhere, with intervals clearly below zero: the discrete model is −3.8 points within 1 min and its average error is +0.20 min. This matches the September 26 search. Training covers only 2025-11-03 to 2026-08-21, so the September test days are days of the year the model never saw, and the time-of-year input pushes it the wrong way. Retry once there is more than a year of data.
- All nine models pass the export, browser and size checks. Three distribution exports needed the 1e-4 float-noise limit (`scripts/finish_recency_export.py`), with no forecast changed. `CURRENT` is unchanged.

This is a report-only rescore of completed model outputs. It uses the same corrected prepared evaluation rows for every condition's fits (control, strong, seasonal), and does not load models or run inference.

**Service dates:** 2026-09-06 through 2026-09-19. **Eligible stop-arrivals:** 4,241,218. **Verification:** complete_with_documented_gaps

These are reused exploratory validation dates, not a fresh independent test. The input features retain present-day timetable patterns; historical timetable-as-of parity is unverified. Historical notice folds can use later records from other training months for earlier rows; this retrospective notice-fold limitation is unchanged.

The comparison has 9 views: fine-tuned median, shared classifier ±3-minute window, and shared classifier discrete median, each under control, strong, seasonal. The window and discrete outputs share the same binned classifier fit within a condition, but select epochs separately using their own frozen criteria; their selected checkpoints may differ.

## Overall metrics

Within rates divide by every eligible arrival. MAE is over scored rows; the report also records any missing coverage.

| Condition | View | Raw/displayed | Rows | Within 1m | Within 3m | Within 5m | MAE min | Missing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| control | Fine median | raw | 4241218 | 51.61% | 78.59% | 87.19% | 2.640 | 0 |
| control | Fine median | displayed | 4241218 | 61.21% | 80.78% | 88.15% | 2.650 | 0 |
| strong | Fine median | raw | 4241218 | 48.97% | 78.41% | 87.30% | 2.609 | 0 |
| strong | Fine median | displayed | 4241218 | 61.55% | 81.06% | 88.43% | 2.623 | 0 |
| seasonal | Fine median | raw | 4241218 | 47.74% | 76.84% | 85.89% | 2.709 | 0 |
| seasonal | Fine median | displayed | 4241218 | 60.47% | 79.37% | 87.05% | 2.743 | 0 |
| control | Distribution window ±3 | raw | 4241218 | 23.62% | 86.04% | 90.42% | 3.386 | 0 |
| control | Distribution window ±3 | displayed | 4241218 | 23.74% | 86.14% | 90.53% | 3.357 | 0 |
| strong | Distribution window ±3 | raw | 4241218 | 23.65% | 86.08% | 90.44% | 3.385 | 0 |
| strong | Distribution window ±3 | displayed | 4241218 | 23.76% | 86.18% | 90.55% | 3.354 | 0 |
| seasonal | Distribution window ±3 | raw | 4241218 | 22.19% | 83.97% | 90.02% | 3.404 | 0 |
| seasonal | Distribution window ±3 | displayed | 4241218 | 22.19% | 83.96% | 90.02% | 3.404 | 0 |
| control | Discrete median | raw | 4241218 | 63.81% | 82.03% | 88.62% | 2.594 | 0 |
| control | Discrete median | displayed | 4241218 | 63.90% | 82.13% | 88.72% | 2.572 | 0 |
| strong | Discrete median | raw | 4241218 | 63.97% | 82.10% | 88.67% | 2.584 | 0 |
| strong | Discrete median | displayed | 4241218 | 64.05% | 82.19% | 88.77% | 2.563 | 0 |
| seasonal | Discrete median | raw | 4241218 | 60.10% | 80.69% | 88.00% | 2.773 | 0 |
| seasonal | Discrete median | displayed | 4241218 | 60.10% | 80.70% | 88.00% | 2.774 | 0 |

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
| strong | fine | verified_1_2 | raw | 4220745 | 87.33% | 2.601 |
| strong | fine | verified_1_2 | displayed | 4220745 | 88.46% | 2.615 |
| strong | fine | unresolved_0_3_4_5 | raw | 20473 | 80.81% | 4.179 |
| strong | fine | unresolved_0_3_4_5 | displayed | 20473 | 82.03% | 4.231 |
| seasonal | fine | verified_1_2 | raw | 4220745 | 85.92% | 2.701 |
| seasonal | fine | verified_1_2 | displayed | 4220745 | 87.08% | 2.735 |
| seasonal | fine | unresolved_0_3_4_5 | raw | 20473 | 79.19% | 4.381 |
| seasonal | fine | unresolved_0_3_4_5 | displayed | 20473 | 80.46% | 4.421 |
| control | window3 | verified_1_2 | raw | 4220745 | 90.45% | 3.379 |
| control | window3 | verified_1_2 | displayed | 4220745 | 90.56% | 3.350 |
| control | window3 | unresolved_0_3_4_5 | raw | 20473 | 84.19% | 4.856 |
| control | window3 | unresolved_0_3_4_5 | displayed | 20473 | 84.15% | 4.869 |
| strong | window3 | verified_1_2 | raw | 4220745 | 90.47% | 3.377 |
| strong | window3 | verified_1_2 | displayed | 4220745 | 90.58% | 3.347 |
| strong | window3 | unresolved_0_3_4_5 | raw | 20473 | 84.13% | 4.860 |
| strong | window3 | unresolved_0_3_4_5 | displayed | 20473 | 84.11% | 4.870 |
| seasonal | window3 | verified_1_2 | raw | 4220745 | 90.05% | 3.397 |
| seasonal | window3 | verified_1_2 | displayed | 4220745 | 90.05% | 3.396 |
| seasonal | window3 | unresolved_0_3_4_5 | raw | 20473 | 83.51% | 4.949 |
| seasonal | window3 | unresolved_0_3_4_5 | displayed | 20473 | 83.50% | 4.949 |
| control | discrete | verified_1_2 | raw | 4220745 | 88.65% | 2.586 |
| control | discrete | verified_1_2 | displayed | 4220745 | 88.75% | 2.564 |
| control | discrete | unresolved_0_3_4_5 | raw | 20473 | 82.40% | 4.190 |
| control | discrete | unresolved_0_3_4_5 | displayed | 20473 | 82.38% | 4.202 |
| strong | discrete | verified_1_2 | raw | 4220745 | 88.70% | 2.576 |
| strong | discrete | verified_1_2 | displayed | 4220745 | 88.80% | 2.555 |
| strong | discrete | unresolved_0_3_4_5 | raw | 20473 | 82.48% | 4.188 |
| strong | discrete | unresolved_0_3_4_5 | displayed | 20473 | 82.49% | 4.199 |
| seasonal | discrete | verified_1_2 | raw | 4220745 | 88.03% | 2.765 |
| seasonal | discrete | verified_1_2 | displayed | 4220745 | 88.03% | 2.766 |
| seasonal | discrete | unresolved_0_3_4_5 | raw | 20473 | 81.58% | 4.425 |
| seasonal | discrete | unresolved_0_3_4_5 | displayed | 20473 | 81.60% | 4.426 |

## Paired service-date bootstrap

Intervals use 2,000 paired resamples of the 14 service dates (seed 4231). Positive within-rate differences favor the condition over control; negative MAE differences favor the condition.

| Condition | View | Raw/displayed | Metric | Condition − control | 95% paired day interval |
| --- | --- | --- | --- | --- | --- |
| strong | fine | raw | within_1_min | -0.02635 | [-0.03027, -0.02331] |
| strong | fine | raw | within_3_min | -0.00186 | [-0.00388, +0.00068] |
| strong | fine | raw | within_5_min | +0.00114 | [-0.00091, +0.00395] |
| strong | fine | raw | mae_scored | -0.03145 | [-0.05786, -0.01242] |
| strong | fine | displayed | within_1_min | +0.00336 | [+0.00171, +0.00516] |
| strong | fine | displayed | within_3_min | +0.00281 | [+0.00083, +0.00530] |
| strong | fine | displayed | within_5_min | +0.00282 | [+0.00092, +0.00524] |
| strong | fine | displayed | mae_scored | -0.02666 | [-0.04413, -0.01274] |
| strong | window3 | raw | within_1_min | +0.00026 | [-0.00009, +0.00067] |
| strong | window3 | raw | within_3_min | +0.00043 | [+0.00015, +0.00077] |
| strong | window3 | raw | within_5_min | +0.00018 | [-0.00008, +0.00049] |
| strong | window3 | raw | mae_scored | -0.00171 | [-0.00796, +0.00391] |
| strong | window3 | displayed | within_1_min | +0.00020 | [-0.00019, +0.00070] |
| strong | window3 | displayed | within_3_min | +0.00041 | [+0.00019, +0.00071] |
| strong | window3 | displayed | within_5_min | +0.00019 | [-0.00000, +0.00048] |
| strong | window3 | displayed | mae_scored | -0.00260 | [-0.00706, +0.00103] |
| strong | discrete | raw | within_1_min | +0.00155 | [+0.00068, +0.00257] |
| strong | discrete | raw | within_3_min | +0.00063 | [+0.00029, +0.00101] |
| strong | discrete | raw | within_5_min | +0.00053 | [+0.00016, +0.00091] |
| strong | discrete | raw | mae_scored | -0.00987 | [-0.01642, -0.00384] |
| strong | discrete | displayed | within_1_min | +0.00153 | [+0.00065, +0.00252] |
| strong | discrete | displayed | within_3_min | +0.00060 | [+0.00028, +0.00096] |
| strong | discrete | displayed | within_5_min | +0.00050 | [+0.00017, +0.00086] |
| strong | discrete | displayed | mae_scored | -0.00878 | [-0.01334, -0.00430] |
| seasonal | fine | raw | within_1_min | -0.03868 | [-0.04946, -0.02946] |
| seasonal | fine | raw | within_3_min | -0.01753 | [-0.02164, -0.01293] |
| seasonal | fine | raw | within_5_min | -0.01301 | [-0.01705, -0.00813] |
| seasonal | fine | raw | mae_scored | +0.06836 | [+0.01655, +0.11204] |
| seasonal | fine | displayed | within_1_min | -0.00741 | [-0.01349, -0.00192] |
| seasonal | fine | displayed | within_3_min | -0.01408 | [-0.01814, -0.00936] |
| seasonal | fine | displayed | within_5_min | -0.01101 | [-0.01469, -0.00643] |
| seasonal | fine | displayed | mae_scored | +0.09328 | [+0.04638, +0.13073] |
| seasonal | window3 | raw | within_1_min | -0.01434 | [-0.01617, -0.01285] |
| seasonal | window3 | raw | within_3_min | -0.02070 | [-0.02553, -0.01510] |
| seasonal | window3 | raw | within_5_min | -0.00402 | [-0.00658, -0.00081] |
| seasonal | window3 | raw | mae_scored | +0.01794 | [-0.02460, +0.05268] |
| seasonal | window3 | displayed | within_1_min | -0.01556 | [-0.01749, -0.01401] |
| seasonal | window3 | displayed | within_3_min | -0.02172 | [-0.02615, -0.01688] |
| seasonal | window3 | displayed | within_5_min | -0.00511 | [-0.00712, -0.00268] |
| seasonal | window3 | displayed | mae_scored | +0.04691 | [+0.02222, +0.06762] |
| seasonal | discrete | raw | within_1_min | -0.03712 | [-0.04369, -0.02914] |
| seasonal | discrete | raw | within_3_min | -0.01340 | [-0.01686, -0.00875] |
| seasonal | discrete | raw | within_5_min | -0.00624 | [-0.00908, -0.00236] |
| seasonal | discrete | raw | mae_scored | +0.17949 | [+0.11495, +0.23132] |
| seasonal | discrete | displayed | within_1_min | -0.03805 | [-0.04439, -0.03034] |
| seasonal | discrete | displayed | within_3_min | -0.01436 | [-0.01726, -0.01051] |
| seasonal | discrete | displayed | within_5_min | -0.00721 | [-0.00950, -0.00405] |
| seasonal | discrete | displayed | mae_scored | +0.20214 | [+0.15746, +0.24081] |

## Group effects

The result JSON contains complete, verified, and unresolved metrics by service date, prepared service type, source, state, and route ID, plus distinct dated-trip counts per service date. Trip-stop IDs were looked up in bounded batches against the read-only database and checked against the prepared service date. Service labels use the frozen prepared segment dictionary; 0 rows differ from the current database train.segment value and are counted in the JSON. A disk-backed preflight found 1 route labels and estimates 2,045,952 bytes for per-group scores; it refuses estimates above 1 GiB. It also reports equal-weight paired effects by dated trip; the confidence interval resamples days, not trips. Route IDs are nullable; 4,241,218 rows ({"tts": 4241218}) lack a route ID and remain in UNKNOWN, so route results exclude attribution for those stops.

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
| service_date | 2026-09-06 | strong | fine | 254765 | 91.83% | 2.026 |
| service_date | 2026-09-07 | strong | fine | 307081 | 89.13% | 2.525 |
| service_date | 2026-09-08 | strong | fine | 319648 | 89.32% | 2.567 |
| service_date | 2026-09-09 | strong | fine | 308324 | 88.07% | 2.683 |
| service_date | 2026-09-10 | strong | fine | 309527 | 88.81% | 2.534 |
| service_date | 2026-09-11 | strong | fine | 304828 | 88.14% | 2.690 |
| service_date | 2026-09-12 | strong | fine | 272970 | 91.34% | 2.153 |
| service_date | 2026-09-13 | strong | fine | 251429 | 90.77% | 2.067 |
| service_date | 2026-09-14 | strong | fine | 285527 | 87.49% | 2.750 |
| service_date | 2026-09-15 | strong | fine | 337845 | 84.95% | 3.254 |
| service_date | 2026-09-16 | strong | fine | 341873 | 86.53% | 3.046 |
| service_date | 2026-09-17 | strong | fine | 336852 | 86.69% | 2.894 |
| service_date | 2026-09-18 | strong | fine | 329338 | 86.81% | 2.846 |
| service_date | 2026-09-19 | strong | fine | 281211 | 90.36% | 2.260 |
| service_date | 2026-09-06 | strong | window3 | 254765 | 93.92% | 2.969 |
| service_date | 2026-09-07 | strong | window3 | 307081 | 91.30% | 3.203 |
| service_date | 2026-09-08 | strong | window3 | 319648 | 91.38% | 3.290 |
| service_date | 2026-09-09 | strong | window3 | 308324 | 90.27% | 3.417 |
| service_date | 2026-09-10 | strong | window3 | 309527 | 90.95% | 3.243 |
| service_date | 2026-09-11 | strong | window3 | 304828 | 90.10% | 3.389 |
| service_date | 2026-09-12 | strong | window3 | 272970 | 93.33% | 2.993 |
| service_date | 2026-09-13 | strong | window3 | 251429 | 93.20% | 2.993 |
| service_date | 2026-09-14 | strong | window3 | 285527 | 89.71% | 3.437 |
| service_date | 2026-09-15 | strong | window3 | 337845 | 87.30% | 3.885 |
| service_date | 2026-09-16 | strong | window3 | 341873 | 88.59% | 3.710 |
| service_date | 2026-09-17 | strong | window3 | 336852 | 88.69% | 3.549 |
| service_date | 2026-09-18 | strong | window3 | 329338 | 89.02% | 3.489 |
| service_date | 2026-09-19 | strong | window3 | 281211 | 92.24% | 3.086 |
| service_date | 2026-09-06 | strong | discrete | 254765 | 92.25% | 1.964 |
| service_date | 2026-09-07 | strong | discrete | 307081 | 89.58% | 2.436 |
| service_date | 2026-09-08 | strong | discrete | 319648 | 89.61% | 2.519 |
| service_date | 2026-09-09 | strong | discrete | 308324 | 88.34% | 2.625 |
| service_date | 2026-09-10 | strong | discrete | 309527 | 89.26% | 2.453 |
| service_date | 2026-09-11 | strong | discrete | 304828 | 88.40% | 2.634 |
| service_date | 2026-09-12 | strong | discrete | 272970 | 91.56% | 2.082 |
| service_date | 2026-09-13 | strong | discrete | 251429 | 91.44% | 1.997 |
| service_date | 2026-09-14 | strong | discrete | 285527 | 87.94% | 2.667 |
| service_date | 2026-09-15 | strong | discrete | 337845 | 85.26% | 3.207 |
| service_date | 2026-09-16 | strong | discrete | 341873 | 86.83% | 2.997 |
| service_date | 2026-09-17 | strong | discrete | 336852 | 86.98% | 2.849 |
| service_date | 2026-09-18 | strong | discrete | 329338 | 87.18% | 2.786 |
| service_date | 2026-09-19 | strong | discrete | 281211 | 90.45% | 2.233 |
| service_date | 2026-09-06 | seasonal | fine | 254765 | 91.72% | 1.977 |
| service_date | 2026-09-07 | seasonal | fine | 307081 | 88.19% | 2.581 |
| service_date | 2026-09-08 | seasonal | fine | 319648 | 87.41% | 2.719 |
| service_date | 2026-09-09 | seasonal | fine | 308324 | 86.45% | 2.830 |
| service_date | 2026-09-10 | seasonal | fine | 309527 | 87.58% | 2.622 |
| service_date | 2026-09-11 | seasonal | fine | 304828 | 86.41% | 2.834 |
| service_date | 2026-09-12 | seasonal | fine | 272970 | 88.95% | 2.402 |
| service_date | 2026-09-13 | seasonal | fine | 251429 | 91.34% | 1.994 |
| service_date | 2026-09-14 | seasonal | fine | 285527 | 86.54% | 2.815 |
| service_date | 2026-09-15 | seasonal | fine | 337845 | 82.75% | 3.463 |
| service_date | 2026-09-16 | seasonal | fine | 341873 | 84.89% | 3.192 |
| service_date | 2026-09-17 | seasonal | fine | 336852 | 85.16% | 3.038 |
| service_date | 2026-09-18 | seasonal | fine | 329338 | 85.20% | 3.016 |
| service_date | 2026-09-19 | seasonal | fine | 281211 | 88.93% | 2.422 |
| service_date | 2026-09-06 | seasonal | window3 | 254765 | 94.21% | 2.926 |
| service_date | 2026-09-07 | seasonal | window3 | 307081 | 90.89% | 3.242 |
| service_date | 2026-09-08 | seasonal | window3 | 319648 | 90.25% | 3.409 |
| service_date | 2026-09-09 | seasonal | window3 | 308324 | 89.69% | 3.475 |
| service_date | 2026-09-10 | seasonal | window3 | 309527 | 90.18% | 3.319 |
| service_date | 2026-09-11 | seasonal | window3 | 304828 | 89.40% | 3.466 |
| service_date | 2026-09-12 | seasonal | window3 | 272970 | 92.93% | 3.052 |
| service_date | 2026-09-13 | seasonal | window3 | 251429 | 93.76% | 2.955 |
| service_date | 2026-09-14 | seasonal | window3 | 285527 | 89.40% | 3.452 |
| service_date | 2026-09-15 | seasonal | window3 | 337845 | 86.36% | 3.961 |
| service_date | 2026-09-16 | seasonal | window3 | 341873 | 87.70% | 3.786 |
| service_date | 2026-09-17 | seasonal | window3 | 336852 | 88.15% | 3.582 |
| service_date | 2026-09-18 | seasonal | window3 | 329338 | 88.31% | 3.571 |
| service_date | 2026-09-19 | seasonal | window3 | 281211 | 91.83% | 3.109 |
| service_date | 2026-09-06 | seasonal | discrete | 254765 | 92.63% | 1.988 |
| service_date | 2026-09-07 | seasonal | discrete | 307081 | 88.65% | 2.582 |
| service_date | 2026-09-08 | seasonal | discrete | 319648 | 88.74% | 2.715 |
| service_date | 2026-09-09 | seasonal | discrete | 308324 | 87.48% | 2.861 |
| service_date | 2026-09-10 | seasonal | discrete | 309527 | 88.11% | 2.690 |
| service_date | 2026-09-11 | seasonal | discrete | 304828 | 87.71% | 2.837 |
| service_date | 2026-09-12 | seasonal | discrete | 272970 | 90.69% | 2.376 |
| service_date | 2026-09-13 | seasonal | discrete | 251429 | 92.33% | 2.018 |
| service_date | 2026-09-14 | seasonal | discrete | 285527 | 86.76% | 2.863 |
| service_date | 2026-09-15 | seasonal | discrete | 337845 | 84.29% | 3.436 |
| service_date | 2026-09-16 | seasonal | discrete | 341873 | 85.55% | 3.297 |
| service_date | 2026-09-17 | seasonal | discrete | 336852 | 86.01% | 3.098 |
| service_date | 2026-09-18 | seasonal | discrete | 329338 | 86.27% | 3.050 |
| service_date | 2026-09-19 | seasonal | discrete | 281211 | 89.64% | 2.521 |
| service | Long-distance | control | fine | 69731 | 50.19% | 10.796 |
| service | Regional | control | fine | 2046223 | 85.56% | 3.097 |
| service | S-Bahn | control | fine | 2125264 | 91.89% | 1.952 |
| service | Long-distance | control | window3 | 69731 | 53.66% | 12.242 |
| service | Regional | control | window3 | 2046223 | 88.25% | 3.677 |
| service | S-Bahn | control | window3 | 2125264 | 93.94% | 2.757 |
| service | Long-distance | control | discrete | 69731 | 51.02% | 10.846 |
| service | Regional | control | discrete | 2046223 | 86.04% | 3.039 |
| service | S-Bahn | control | discrete | 2125264 | 92.54% | 1.850 |
| service | Long-distance | strong | fine | 69731 | 50.24% | 10.774 |
| service | Regional | strong | fine | 2046223 | 85.66% | 3.082 |
| service | S-Bahn | strong | fine | 2125264 | 92.35% | 1.914 |
| service | Long-distance | strong | window3 | 69731 | 53.76% | 12.219 |
| service | Regional | strong | window3 | 2046223 | 88.26% | 3.673 |
| service | S-Bahn | strong | window3 | 2125264 | 93.97% | 2.757 |
| service | Long-distance | strong | discrete | 69731 | 51.20% | 10.797 |
| service | Regional | strong | discrete | 2046223 | 86.12% | 3.025 |
| service | S-Bahn | strong | discrete | 2125264 | 92.55% | 1.848 |
| service | Long-distance | seasonal | fine | 69731 | 46.69% | 11.175 |
| service | Regional | seasonal | fine | 2046223 | 84.16% | 3.217 |
| service | S-Bahn | seasonal | fine | 2125264 | 91.15% | 2.009 |
| service | Long-distance | seasonal | window3 | 69731 | 53.72% | 11.810 |
| service | Regional | seasonal | window3 | 2046223 | 87.86% | 3.676 |
| service | S-Bahn | seasonal | window3 | 2125264 | 93.30% | 2.866 |
| service | Long-distance | seasonal | discrete | 69731 | 52.54% | 11.456 |
| service | Regional | seasonal | discrete | 2046223 | 85.37% | 3.173 |
| service | S-Bahn | seasonal | discrete | 2125264 | 91.69% | 2.105 |

## Export, browser, and resources

Every candidate is gated on successful ONNX export parity and a passed actual Chromium browser check. Artifact size includes the full manifest and model file; the strict target is 35,000,000 bytes.

| Condition | Fit/view | Manifest-inclusive bytes | ≤35 MB | Browser pass | Cgroup peak bytes |
| --- | --- | --- | --- | --- | --- |
| control | fine | 15859352 | True | True | 3311386624 |
| control | distribution | 16161154 | True | True | 3955208192 |
| control | discrete | 16162104 | True | True | 1617715200 |
| strong | fine | 15859368 | True | True | 3846311936 |
| strong | distribution | 16161151 | True | True | 1366003712 |
| strong | discrete | 16162102 | True | True | 1618616320 |
| seasonal | fine | 15863629 | True | True | 3320967168 |
| seasonal | distribution | 16165407 | True | True | 1488437248 |
| seasonal | discrete | 16166365 | True | True | 1618337792 |

## Full later-window quantile diagnostics

These are the frozen producers’ full later-window diagnostics, not values reconstructed from point arrays. Producer scoring validates finite ordered percentile triplets; each quantile denominator must equal the eligible cohort. Discrete metrics come from full-cohort ONNX CPU replay. Point-only saved arrays cannot independently reproduce interval coverage.

| Condition | View | Quantile-scored | P25–P75 coverage | At/below P25 | At/below P75 |
| --- | --- | --- | --- | --- | --- |
| control | fine | 4241218 | 51.18% | 25.59% | 76.77% |
| control | distribution | 4241218 | 31.24% | 47.82% | 79.06% |
| control | discrete | 4241218 | 71.25% | 47.82% | 79.06% |
| strong | fine | 4241218 | 56.89% | 17.22% | 74.11% |
| strong | distribution | 4241218 | 31.34% | 47.80% | 79.14% |
| strong | discrete | 4241218 | 71.51% | 47.81% | 79.14% |
| seasonal | fine | 4241218 | 49.47% | 20.85% | 70.32% |
| seasonal | distribution | 4241218 | 28.54% | 53.72% | 82.26% |
| seasonal | discrete | 4241218 | 63.09% | 51.88% | 77.60% |

## Paired dated-trip effects

Each dated trip is one cluster; the table reports equal-cluster means over the corrected September cohort. Within-five gains are positive; MAE reductions are negative.

| Condition | View | Metric | Control | Condition | Condition − control | Clusters improved |
| --- | --- | --- | --- | --- | --- | --- |
| strong | fine | within_5_min_displayed | 0.87310 | 0.87472 | +0.00162 | 2.2% |
| strong | fine | mae_displayed | 2.85662 | 2.83802 | -0.01860 | 23.0% |
| strong | window3 | within_5_min_displayed | 0.89662 | 0.89695 | +0.00032 | 1.0% |
| strong | window3 | mae_displayed | 3.56933 | 3.56465 | -0.00468 | 19.7% |
| strong | discrete | within_5_min_displayed | 0.87756 | 0.87857 | +0.00100 | 2.7% |
| strong | discrete | mae_displayed | 2.79693 | 2.78076 | -0.01618 | 28.3% |
| seasonal | fine | within_5_min_displayed | 0.87310 | 0.86327 | -0.00983 | 5.1% |
| seasonal | fine | mae_displayed | 2.85662 | 2.92590 | +0.06928 | 32.3% |
| seasonal | window3 | within_5_min_displayed | 0.89662 | 0.89396 | -0.00267 | 2.9% |
| seasonal | window3 | mae_displayed | 3.56933 | 3.56296 | -0.00637 | 29.8% |
| seasonal | discrete | within_5_min_displayed | 0.87756 | 0.87391 | -0.00366 | 5.3% |
| seasonal | discrete | mae_displayed | 2.79693 | 2.92769 | +0.13075 | 32.2% |

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
