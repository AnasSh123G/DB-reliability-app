---
kind: spec
title: "Compact delay-model training plan"
---

Authorized on 2026-10-13: proceed with the investigation's recommended training and testing, keep work inside DB-reliability-app, and do not commit. Working directory: `/home/anas/dev/DB-reliability-app`. Earlier research scripts and binary results have been moved here; the initial report is `docs/compact-model-investigation.md`.

## Objective and boundaries

Train useful challengers to the existing compact arrival MLP, rather than only altering its displayed point. Prioritize higher within-five-minute accuracy with preserved or improved MAE; also measure the distribution model's explicit tolerance/precision tradeoff. No deployment or promotion is part of this experiment. Keep the public API and forecast schema unchanged.

All new source, reports, logs, temporary files, checkpoints and exports live under this repository. Reuse the existing read-only prepared-input cache (already linked from the repository to `/media/E`) rather than duplicate tens of gigabytes. The shared source database is read-only.

## Fixed comparison

| Role | Dates / rows | Use |
|---|---|---|
| Training | 2025-11-03..2026-08-22, 88,948,878 arrivals | Full history, existing no-season vocabulary/scaling/notice rates |
| Selection | 2026-08-23..09-05 | Select epochs and freeze checkpoints |
| Later validation probe | 2026-09-06..19, 4,241,218 arrivals | Compare frozen candidates on identical rows |
| Previously inspected final test | 2026-09-20..26 | Do not use for tuning or claim it as a new independent test |

The original search already used the whole validation period. The split above limits further tuning, but cannot make those dates independent. Only one partial later day exists in the database, so a fresh complete test week is currently unavailable. Do not edit `inspected_dates.json` or simulate an independent test.

The reference is the original search checkpoint `f1ac12b1742e3a0b`, trained only through August 22. The **deployed final-refit checkpoint must not initialize these runs**, because it has trained on the validation dates.

## Initial run order

| Run | Mechanism | Epoch selection | Expected download |
|---|---|---|---|
| Memory/speed probes | Two small row counts, widest network and distribution head, one epoch each | No accuracy claims | Probe only |
| Fine-tuned MLP | Existing width-512 checkpoint, six more epochs, maximum LR 0.0003 | Five-minute accuracy subject to selection MAE no worse than reference; fallback lowest MAE | About 16 MB |
| Wider MLP | Width 1024, three residual blocks, existing train embedding width 32, fresh initialization, 12 epochs | Same precision constraint and fallback | About 35 MB; measure actual export |
| No-season distribution MLP | Width 512, 150 one-minute bins, fresh initialization, 12 epochs | Five-minute accuracy using existing ±3-minute window | About 16 MB |

These are bounded experiments, not claims that each will improve. A training failure, size miss, or poor selection result can justify reducing a run or skipping additional variants. If results warrant it, try a second seed or a small ensemble; only spend that compute when it addresses a measured gap. Further tree/distillation work waits for a tree teacher that wins offline.

The distribution run will report median, ±3-minute and ±5-minute decisions together, without disguising a window point as p50. Existing serving supports the ±3 rule. A ±5 rule would need consistent metadata handling in artifact writing and Python prediction before future serving; this experiment can still test it offline.

Follow-up hypothesis, fixed before scoring the new distribution weights: all 7,967,853 validation targets were checked and are exact whole minutes. The current distribution predictor interpolates uniform density inside each one-minute bin. After the three fits, also test discrete conditional p25/p50/p75 using each CDF's first class reaching the percentile. This needs no further training or new inputs and can export `[N,3]` quantiles using the existing frontend/API. It may improve point precision by avoiding within-bin interpolation; improvement is unproven until scored. Preserve the source network's binned training configuration and record the inference transform separately. A synthetic pre-evaluation review found cross-runtime rounding at exact CDF ties; use a fixed probability tolerance of `1e-6` (first class reaching `q−1e-6`) across NumPy/Torch/ONNX, and score the entire validation through the exported ONNX CPU graph.

The final comparison will report both raw floating-point scores and the unchanged app display rule (`point<1 -> 0`, otherwise nearest whole minute). This clarifies tolerance-boundary effects without changing the app.

Subsequent bounded scalar-column audit confirmed that all 88,948,878 training labels and all 7,967,853 validation labels are finite whole-minute values. No input matrix or trained model was loaded for this check; the 262,144-row scan used a separate one-GiB cap and peaked at 24 MiB. Evidence is saved in `data/ml/compact_training_20261013/target-resolution.json`.

## Resource safeguards

- One training/evaluation job at a time in its own systemd user service: `MemoryMax=8G`, `MemoryHigh=5G`, `MemorySwapMax=0`, four CPU threads, low CPU priority. Re-check available memory before launch; keep at least four GiB beyond the hard cap.
- Two small memory/speed probes before full fitting. Project memory and elapsed time; keep at least 15% cap headroom. If a probe fails, reduce batches or block sizes first.
- Read prepared inputs in bounded blocks, never map or copy all 89 million rows into a GPU or an in-memory matrix. Release/reclaim file-cache pages after reading blocks where supported.
- Check tensor sizes against the existing ROCm two-GiB per-tensor limit. Limit GPU allocator use to 60% of device memory, and record CPU/cgroup/GPU peaks separately.
- Save resumable epoch checkpoints under a setup fingerprint. Refuse to resume if source weights, prep/vocabulary, inputs, architecture, selection policy, or run parameters changed.

## Verification and completion

Measure within-one/three/five, MAE, coverage, quantile ordering/calibration, and per-segment/day results. Compare all candidates against the same reference rows. Export viable finalists to offline model directories; measure ONNX plus manifest size against a flexible 36 MB experimental ceiling, while clearly identifying anything above the app's strict 35 MB promotion budget.

Use existing ONNX export checks and the unchanged browser predictor with the existing model endpoint paths. Verify Python/ONNX/browser agreement for representative known and unknown category inputs, single calls and batches. Run relevant training and deployment tests and obtain a fresh review of substantive training changes.

Write the resulting measurements and recommendations into `docs/compact-training-results.md`. Keep `models/v2/CURRENT` unchanged and leave all changes uncommitted.
