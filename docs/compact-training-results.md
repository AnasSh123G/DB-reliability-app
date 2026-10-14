---
kind: spec
title: "Compact delay-model training results"
---

Full-history training authorized on 2026-10-13. All work is inside DB-reliability-app; no commits or promotions.

Candidates are trained on 88,948,878 arrivals through 2026-08-22, selected on the first validation fortnight, and compared below on the identical 4,241,218 arrivals of September 6–19. The original search already inspected this validation period. These are exploratory validation measurements, not new independent test results.

Recommendation: **discrete conditional quantiles** are the best balanced candidate for the unchanged app display behavior. The 16.16 MB model improves displayed five-minute accuracy from 87.59% to 88.29% and displayed MAE from 2.932 to 2.874 minutes. Its raw MAE is slightly worse (2.720 versus 2.626). Use it as the next independent-validation candidate, rather than claiming a verified production replacement.

[Training plan](compact-training-plan.md) · [Initial investigation](compact-model-investigation.md) · [Independent source review](compact-training-review.md) · [Postprocessing review](compact-postprocess-review.md)

| Frozen model / decision | Within 5 min | Within 3 min | MAE, min | App display: within 5 | App display: MAE |
|---|---:|---:|---:|---:|---:|
| reference_median | 87.38% | 78.92% | 2.6258 | 87.59% | 2.9320 |
| fine_median | 87.51% | 79.07% | 2.6212 | 87.70% | 2.9273 |
| wide_median | 87.44% | 79.36% | 2.6143 | 87.61% | 2.9090 |
| distribution_median | 88.00% | 79.10% | 2.8644 | 88.70% | 2.9415 |
| distribution_window3 | 90.14% | 85.45% | 3.5497 | 89.75% | 3.6851 |
| distribution_window5 | 91.65% | 35.46% | 4.6043 | 91.25% | 4.7269 |
| discrete_median | 88.66% | 82.04% | 2.7200 | 88.29% | 2.8739 |

The raw columns use the existing continuous-output scoring rule. Displayed columns apply the app's unchanged rule: predictions below one minute display zero, and the rest round to whole minutes. This distinction matters when an output lies close to the edge of the five-minute tolerance.

Discrete quantiles use the frozen distribution network, one-minute class labels and a fixed 1e-6 CDF boundary tolerance. Its entire validation is scored through the exported ONNX CPU graph. The training head and loss remain binned; metadata separately records the quantile inference transform.

| Candidate | Best epoch | Model plus manifest | Strict 35 MB budget | Browser parity | Cgroup peak | GPU allocation peak |
|---|---:|---:|---|---|---:|---:|
| fine | 6 | 15.858 MB | yes | pass | 4.298 GiB | 416.1 MiB |
| wide | 12 | 35.173 MB | no; comparison only | pass | 4.298 GiB | 692.0 MiB |
| distribution | 10 | 16.176 MB | yes | pass | 4.342 GiB | 422.9 MiB |
| discrete | 10 | 16.161 MB | yes | pass | 1.029 GiB | 0.0 MiB |

Offline artifact directories (job swap was disabled):

- `fine`: `data/ml/compact_training_20261013/runs/fine/artifacts/20261013T095540Z`
- `wide`: `data/ml/compact_training_20261013/runs/wide/artifacts/20261013T103426Z`
- `distribution`: `data/ml/compact_training_20261013/runs/distribution/artifacts/20261013T110120Z`
- `discrete`: `data/ml/compact_training_20261013/runs/discrete/artifacts/20261013T110324Z`


The quantile head preserves ordered p25/p50/p75. A distribution window point is deliberately different from its median; gains in a wide tolerance score can come with worse point precision. Quantile diagnostics and segment/day breakdowns are saved in each result JSON. Inclusive empirical quantile coverage can depart from nominal percentages because the recorded outcomes contain many minute-valued ties.

Paired day-bootstrap intervals in `summary.json` account for variation across these fourteen days but do not remove selection bias or establish future/general-year performance. No newer complete untouched test week is available.

All fits ran serially under MemoryMax=8G, MemoryHigh=5G, MemorySwapMax=0 and four CPU threads. Epoch checkpoints preserve model, optimizer, schedule and RNG state; CPU interruption/resume tests passed. 99 targeted training, decision-rule and serving tests passed (49 CPU training/postprocessing tests and 50 existing feature/notice-rate/deployment tests); review details are linked above. GPU crash/resume is not certified by the CPU test. The deployed pointer remains `20261004T213106Z`.

## Choosing the model

| Priority | Candidate | Decision |
|---|---|---|
| Balanced point forecasts in the existing UI | Discrete quantiles, 16.16 MB | Recommended next candidate: better displayed MAE, within-one/three/five accuracy, and the same ordered quantile API. Raw MAE increases by about 0.094 minute. |
| Preserve raw MAE and use the smallest change | Fine-tuned MLP, 15.86 MB | Small improvement: +0.13 percentage point within five minutes, with slightly lower raw MAE. |
| Prioritize the five-minute tolerance score | Distribution with the existing ±3-minute window, 16.18 MB | 90.14% raw / 89.75% displayed within five minutes, with appreciably worse MAE and one-minute accuracy. |
| Maximize the tolerance score regardless of precision | ±5-minute window, same weights | 91.65% raw, but MAE 4.60 minutes and within-three accuracy only 35.46%. Offline diagnostic; the Python predictor and artifact writer currently hardcode a three-minute half-window. |
| Spend the flexible size ceiling on a larger network | Wider MLP, 35.17 MB | Best raw MAE among the fitted quantile MLPs, but the gain is small and it misses the strict 35,000,000-byte gate. Further widening is not the preferred next experiment. |

The ±5-minute browser fixture passed, but there is no production-compatible ±5 artifact here: the exported probability artifact is configured for ±3. A future ±5 option needs consistent window metadata in Python inference and artifact writing; the endpoint and forecast shapes can remain the same. A window point is a high-mass interval centre, not p50.

![Accuracy and precision frontier](../data/ml/compact_training_20261013/accuracy-precision.svg)

## Point precision and service segments

The discrete model improves raw within-one-minute accuracy by 10.06 percentage points; the improvement after the app display transform is 1.50 points. Both views are shown to avoid attributing a rounding/clamping effect entirely to neural training.

| Decision | Raw within 1 min | Displayed within 1 min | Regional within 5 | Long-distance within 5 | S-Bahn within 5 |
|---|---:|---:|---:|---:|---:|
| reference_median | 52.87% | 60.94% | 84.24% | 46.73% | 91.73% |
| fine_median | 52.49% | 61.09% | 84.38% | 46.88% | 91.85% |
| wide_median | 52.88% | 61.82% | 84.33% | 46.96% | 91.77% |
| distribution_median | 44.49% | 57.49% | 84.86% | 47.57% | 92.35% |
| distribution_window3 | 22.73% | 22.61% | 87.39% | 52.36% | 94.03% |
| distribution_window5 | 11.50% | 11.45% | 89.36% | 54.35% | 95.09% |
| discrete_median | 62.94% | 62.43% | 85.62% | 50.62% | 92.83% |

Long-distance arrivals account for only 69,731 of the 4,241,218 comparison rows (about 1.64%). Discrete quantiles improve their five-minute accuracy from 46.73% to 50.62%, but their MAE remains about 11.13 minutes. The aggregate score therefore cannot describe every service equally well. Cancellation prediction and arrivals without recorded outcomes remain outside this evaluation.

## Completed checks and resource use

- Three full-history fits completed serially in 1 hour 19 minutes. The shared training service peaked at **4.342 GiB** against its eight-GiB hard cap and used **zero job swap**. Cgroup peaks include charged filesystem cache and are cumulative within that serial service; per-process RSS and GPU allocation are separately saved. Largest GPU allocation was about 692 MiB.
- Discrete quantiles scored all 7,967,853 validation arrivals through the exported ONNX CPU graph in about 87 seconds. Its separate cgroup peaked at 1.029 GiB, with zero job swap and no GPU tensor allocation.
- Actual Chromium/ONNX Runtime Web 1.30.0 checks passed for every candidate: 512 known-category and 64 UNKNOWN-category rows in batches, plus 21 single calls including UNKNOWN. Discrete outputs matched exactly; the largest other difference was 0.000260 minute, below the 0.001-minute gate. Singles and batches agreed exactly.
- Browser jobs ran serially under two-GiB caps and peaked at roughly 488–564 MiB, with zero job swap. Desktop single-call p95 was 0.3–0.8 ms; local load-plus-batch checks exclude real network download and do not certify mobile performance.
- All exported copies match their tested model hashes. Production browser metadata, input tensor names/dtypes/widths, output contracts, and actual ONNX-plus-manifest byte counts passed a final capped check. Evidence is in `data/ml/compact_training_20261013/verification.json`.
- A bounded audit found no non-integer or nonfinite labels in the full training or validation data. `target-resolution.json` records that check. The discrete tail classes censor outcomes outside the trained -30..119-minute labels; their limitation is retained in the manifest.
- The source and postprocessing reviews are linked above. The Chromium harness uses short repository-local scratch paths to respect Unix socket limits and retains launch diagnostics. Timeout cleanup is covered by the reviewer’s synthetic lifecycle checks.

All new reports, scripts, tests, model exports, checkpoints, logs and task temporary files are in `/home/anas/dev/DB-reliability-app`. The pre-existing prepared-data symlink is reused read-only. The application code and deployed `models/v2/CURRENT` are unchanged, and no commits were made.

## What should follow

Validate the discrete candidate on newer untouched complete service dates before promotion. The database currently has only a partial September 27 after the already inspected test week, so it cannot establish a fresh independent result today. Historical timetable-as-of availability is also unverified. Refit a validated winner on the latest permitted history before making it the app default.

For further model work, prioritize discrete-distribution selection tuned for point precision, a second seed only if it moves the measured frontier, and separate long-distance objectives or heads. Existing TabM trials and the sampled LightGBM pilot did not beat the MLP locally. A compact tree teacher, categorical encoding or distillation remains worthwhile only after a teacher wins a chronological comparison. Details and primary references are in the initial investigation.
