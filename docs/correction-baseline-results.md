# Correction coverage and unchanged-model baselines

The dated-schedule correction pass processed all 320 prepared dates. The corrected data preserves the original ordered rows and input features. Source schedules change targets only where the raw plan and post-event changed-time evidence support them; unsupported targets remain unchanged and flagged.

| Cohort | Arrivals | Source verified | Corrected labels |
| --- | ---: | ---: | ---: |
| Training | 88,948,878 | 67.06% | 4,703,903 |
| All validation | 7,967,853 | 87.80% | 656,276 |
| Later evaluation, Sep 6–19 | 4,241,218 | 99.52% | 392,497 |

The largest unresolved group is incomplete declared archive coverage: 29,086,734 training and 934,944 validation rows. These rows were preserved. The final comparison reports verified and unresolved subsets separately.

Across training and validation, **460,966** corrected labels moved from more than five minutes early to a nonnegative delay; another **12,565** moved into the −5 to 0 minute range. The producer’s legacy counter named `corrected_negative_to_nonnegative` combines those two categories. A further 250 corrected labels lie outside the old ±120-minute bound and are retained.

## Same predictions, corrected targets

The following results rescore unchanged original predictions on the same 4,241,218 later evaluation arrivals. They measure relabelling effects; they are not results from the new corrected-data fits.

| Unchanged model | Within 5 min: original labels | Within 5 min: corrected labels | Corrected app-displayed within 5 min | Corrected raw MAE, minutes |
| --- | ---: | ---: | ---: | ---: |
| Current MLP reference | 87.38% | 84.74% | 86.32% | 3.158 |
| Previous compact MLP | 87.51% | 84.82% | 86.41% | 3.163 |
| Previous discrete classifier | 88.66% | 86.30% | 87.04% | 3.007 |
| Previous ±3-minute window classifier | 90.14% | 87.76% | 88.40% | 3.812 |

**The old 87.38% reference accuracy becomes 84.74% against corrected targets.** New model gains must be measured against the corrected-target baselines, not the earlier score on different labels. The app display clamps sub-minute/negative predictions and rounds positive values, so displayed scores are reported separately.

The window classifier has the highest within-five score here, but worse MAE and only 22.19% raw within-one-minute accuracy. The discrete classifier has 60.27% raw within-one accuracy and lower MAE than the MLP reference. The final recommendation must retain these precision tradeoffs.

## September 15 versus August only

The [corrected comparison](corrected-august-comparison.md) uses August 1–31 exclusively, one vote per accepted run date over fixed common stops. Across 24,252 supported recurring trips, the mean deviation is **+0.63 minutes** and the median **−0.40 minutes**. **10.71%** are more than five minutes worse than their August mean; **2.99%** are more than fifteen minutes worse. This is a supported, partially verified cohort and does not establish a construction cause.

## Execution and provenance

Corrected prepared-data/evidence verification and both pre-fit comparison stages passed their bounded execution gates. Correction peak was 1,881,395,200 bytes (about 1.75 GiB), with zero job swap. The independent streamed label/status audit used 39.5 MB and zero swap under a 512 MiB cap. Both unweighted fits and all three browser checks have completed. Recency-weighted fits and the paired/subgroup comparison remain pending. The following are preliminary results on the same corrected later evaluation arrivals.

[Correction/status audit](../data/ml/trip_schedule_correction_20261013/coverage-summary.json) · [Unchanged-model scores and identities](../data/ml/recency_training_20261013/correction_baselines/result.json) · [Open-task tracker](delay-model-open-tasks.md)


## Corrected-data unweighted controls — preliminary

| Output | Raw within 5 min | Raw within 1 min | Raw MAE, minutes | Complete download, MB |
| --- | ---: | ---: | ---: | ---: |
| Fine MLP | 87.50% | 51.52% | 2.617 | 15.86 |
| Discrete classifier | 88.90% | 64.32% | 2.549 | 16.16 |
| ±3-minute window classifier | 90.69% | 22.82% | 3.371 | 16.16 |

These outputs passed native/ONNX parity and single/batch actual-browser checks with the pinned Chromium and ONNX Runtime Web 1.30.0. The classifier views share a fit and independently select checkpoints using selection dates only. All later scores cover the same 4,241,218 arrivals with full prediction coverage. The discrete classifier has the strongest precision/MAE balance so far; the window output trades precision for within-five accuracy. Recency effects, subgroup changes, interval calibration and paired uncertainty still require the final report. No candidate is deployed.


## First recency pair — preliminary

The weighted fine MLP completed on identical corrected data and the same training budget. Later raw within-five accuracy is **87.41%**, versus **87.50%** unweighted (−0.094 percentage points); within-one accuracy is **49.57%**, versus **51.52%** (−1.946 points). MAE is nearly unchanged: **2.615** versus **2.617 minutes**. This pair does not currently support adopting the recency ramp for the MLP. Paired day uncertainty, subgroup effects and weighted browser checks remain pending. The weighted classifier is still fitting; this MLP result does not determine its outcome.


## Recency interpretation withdrawn — batching defect

The preliminary statement that the recency ramp did not help the MLP is withdrawn as a conclusion about the requested policy. The implementation divided weighted loss by each mini-batch's own weight sum, while training shuffled rows only within contiguous date-local blocks. A constant multiplier cancels exactly. The streamed [layout audit](../data/ml/recency_training_20261013/weight-layout-audit.json) found 309 of 340 constant-weight blocks; recent blocks' normalized weight range is only approximately 0.986–1.015. The stored scores and browser checks are real, but their weighted/unweighted contrast does not implement the intended temporal objective. A new matched comparison must mix dates within bounded pools and use one fixed dataset normalization. Original correction-baseline and August results remain valid.
