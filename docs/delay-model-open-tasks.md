---
kind: spec
title: "Open tasks: improve delay-prediction models"
---

# Delay-model task tracker

**Main objective:** find compact browser-compatible models that improve delay prediction on an honest, comparable evaluation. Correcting labels and fixing ingestion are supporting work; passing their tests does not establish a model improvement.

This is the canonical open-task list. Update it when a task completes, fails, resumes or changes scope. Mark completion only with saved evidence. The implementation policy and resource constraints remain in [the specification](delay-recency-and-cause-spec.md).

## Latest verified state

Checked October 14, 2026, 06:54 Europe/Berlin.

- The correction processed **all 320 dates**, changing **5,360,179 labels**. Full corrected-data/evidence verification passed before comparison. The original source database and production model remain unchanged.
- Source evidence verifies **67.06% of training rows** and **99.52% of the later evaluation rows**. Unsupported rows retain original labels and explicit unresolved flags; completion does not mean every label was verified.
- September 15 versus August 1–31 is complete: 24,252 supported recurring trips, mean **+0.63 minutes**, median **−0.40 minutes**, and **10.71%** more than five minutes worse. [Comparison and coverage](corrected-august-comparison.md).
- Unchanged-model correction baselines are complete. Current MLP raw within-five accuracy is **84.74% against corrected labels**, versus its prior 87.38% against original labels. [Baseline evidence](correction-baseline-results.md).
- All six first-attempt outputs finished and passed browser checks. **The recency comparison is invalid as a test of the requested weighting**: per-batch normalization cancels near-constant weights inside date-local blocks. Audit: 309/340 constant-weight blocks; effective weights within recent blocks approximately 0.986–1.015 instead of the intended 1–1.5. Preserve these artifacts as a diagnosed attempt; do not use their tiny weighted/unweighted differences to conclude recency is ineffective.
- The first-attempt final report completed and is explicitly marked inconclusive for recency. Fixed dataset normalization and eight-span date mixing are implemented; **62 CPU tests pass**, including gradients and interrupted/resumed training. Two-full-pool GPU probes passed within about **2.3 GiB / 1.9 GiB** private memory, zero swap. The [independent delta review](recency-weighting-normalization-review.md) passed. Readiness is active; all 39 reused-input identities passed. Corrected-run unweighted MLP completed with **87.19%** later raw within-five accuracy and **2.640-minute** MAE. Full-fit peak was **4.31 GiB**, zero job swap. Both unweighted fits and discrete export completed. Fine and discrete browser checks passed. Distribution browser stopped the queue: an interpolated p25 differs 0.272 seconds across native/Web inference; window points and all 576 rounded fixture outputs match. Identical-input decoder comparisons agree to machine precision. Split numerical/semantic parity diagnosis, tests and review are in progress before resuming weighted fits. Rerun controls and weighted fits with identical bounded date mixing and one fixed dataset normalization; retain the same top-three outputs, corrected data, seeds and budgets.
- Permanent ingestion implementation and independent review are complete: **232 tests pass**, **87% combined statement/branch coverage**. Ambiguous actual DST sides abstain; receipt inserts use bounded index-backed batches. [Review and limits](dated-schedule-ingest-review.md). Real-database migration remains deferred.
- First-attempt readiness had **40 passing CPU tests**. New candidate readiness records **62 passing CPU tests**, updated source fingerprints and the same exact Chromium/runtime pin; its independent review gate passed and its serial controller is active. Fits remain under an 8 GiB cap with zero job swap; the completed MLP peaked at about 5.00 GiB including file cache.

## Open tasks, in execution order

| ID | Task | Status / dependency | Completion evidence |
|---|---|---|---|
| T07 | Run corrected-data unweighted controls and recency-weighted fits for the **top three candidate outputs only**. | First attempt completed; reopen for corrected temporal weighting. Fixed weighting/mixing implementation, 62 passing CPU tests and both memory probes complete. Independent delta review passed; serial controller active in `data/ml/recency_training_mixed_20261014`; 39 reused prerequisites passed, both unweighted fits and discrete export complete. Distribution-browser numerical parity gate failed; diagnosis/review in progress. Weighted pair not started; preserve checkpoints. | Four underlying fits: fine MLP and shared classifier per condition. Three outputs per condition: fine quantiles, discrete classifier quantiles, and ±3-minute window prediction. |
| T08 | Test whether the approved recency ramp improves performance: weight 1 for older trips, gradually increasing to 1.5 during the final calendar month of actual fit dates, July 21–August 21. | First-attempt differences are inconclusive because weights cancel inside date-local batches; correct sampling/normalization and rerun matched pairs. | Comparable selection/later-window metrics and paired day-cluster uncertainty; preserve exactness and tail tradeoffs. |
| T09 | Validate all six exported candidate artifacts through ONNX and the pinned actual browser, including single/batch and UNKNOWN-category inputs, unchanged endpoints/output semantics, size and resource usage. | First-attempt six artifacts passed. Corrected run fine/discrete pass, distribution failed combined 1e-3-minute tolerance; diagnosed p25 interpolation amplifies a tiny probability/CDF difference. Review a separate raw/decoder/semantic protocol before resuming. No window decision or rounded fixture change observed. | Native/export/browser parity gates and manifest-inclusive download sizes, targeting at most about 35 MB. |
| T10 | Produce the final comparison and recommendation: which candidate is better, where it improves or regresses, and whether any gain survives corrected labels and subgroup checks. | First-attempt report preserved with an explicit invalid-recency warning. Final recommendation must use the corrected weighting experiment. | `docs/recency-training-results.md` with within-5 accuracy, within-1/3 accuracy, MAE, calibration, coverage and limitations. No improvement claim until this evidence exists. |
| T11 | Reassess delay-cause/construction features if the controlled comparison shows remaining supported regional/time shifts. Establish when cause information was available before using it as an input. | Follow-on investigation, separate from the recency comparison. Existing historical cause rates already exist; direct cause inputs are not certified as available at forecast time. | Cause availability/coverage audit and, if warranted, a separately controlled experiment preserving browser contracts. |
| T12 | Activate the additive dated-schedule schema and updated collection after the frozen-data model comparison no longer needs the original database bytes. Historical replay is a separately scoped operation. | Deferred until T10; no production migration or collection restart has been performed. | Successful migration/collection checks and monitoring of missing/conflicting dated references. |
| T13 | Harden the transport policy marker for a future correction resume: atomic write and unambiguous “eight attempts / seven retries” wording. | Low priority; correction has finished, but the active comparison still fingerprints the wrapper. Defer edits until the corrected comparison completes. | Focused retry/policy tests and a versioned transport policy after the active comparison finishes. |

## Completed foundations

| Closed task | Evidence / retained boundary |
|---|---|
| **T01 — Permanent ingestion implementation and independent review** | 232 tests, 87% combined coverage; independent 43-contract recheck and bounded append probe. Reviewed hashes match current code. [Review and limits](dated-schedule-ingest-review.md). T12 activation remains open. |
| **T04 — Controlled-comparison readiness** | First-attempt 40-test readiness is preserved. Corrected version readiness has62 passing CPU tests, two full-pool probes and [independent delta review](recency-weighting-normalization-review.md). Exact Chromium/runtime identity retained; 39 reused prerequisite files pinned. |
| **T02 — Full correction pass** | All 320 dates committed; complete `data/ml/trip_schedule_correction_20261013/result.json`; producer peak about 1.75 GiB, zero swap. Original DB preserved. |
| **T03 — Corrected cache verification and coverage audit** | Both pre-fit stages accepted `verify_correction_version.py`; `coverage-summary.json` records unchanged unsupported rows and verified/corrected populations. |
| **T05 — Corrected September 15 / August-only comparison** | [Report](corrected-august-comparison.md), saved trip/stop CSVs, plots and source identities under `corrected_august_comparison/`. |
| **T06 — Unchanged-model correction baselines** | [Report](correction-baseline-results.md), saved ordered predictions and identity-bound original/corrected scores under `correction_baselines/`. |

- [x] Explore current browser inference, model sizes and compatible alternatives; save the initial compact-model comparison.
- [x] Diagnose where prediction misses concentrate before introducing recency weighting.
- [x] Audit suspicious early labels against raw planned and changed timestamps; identify recurring-schedule reuse in ingestion.
- [x] Establish the previous-calendar-month-only comparison and correction policy; verify the three-date pilot.
- [x] Implement memory-bounded correction, resume, verification, comparison, training, export/browser and reporting stages.
- [x] Implement permanent dated-plan ingestion with regression tests for both polling and finalization, revision conflicts, receipt cutoffs, partial updates, replay, DST and cancellations.
- [x] Pin the pending comparison's Chromium path/hash/version and ONNX Runtime Web 1.30.0; add CPU tests for the browser identity gates.

## Durable evidence and operational pointers

- Specification: [delay-recency-and-cause-spec.md](delay-recency-and-cause-spec.md).
- Correction progress: `data/ml/trip_schedule_correction_20261013/progress.json`; transport log: `full-resume.log` in the same directory.
- Ingestion tests/coverage/source hashes: `data/ml/dated_schedule_ingest_20261014/verification.json` and `coverage.json`.
- First-attempt comparison readiness/tests/browser pin: `data/ml/recency_training_20261013/`; `readiness.json` records the passed comparison review and exact tested sources. Future-ingestion activation remains separate from that readiness scope.
- The supervisor `dbrel-recency-supervisor-20261013.service` accepted full correction, reviewed readiness and correction-service exit and ran the first serial comparison. Preserve its outputs; its weighting flaw requires a reviewed new comparison version. A failed or incomplete stage must stop dependent work and remain open in this tracker.
- Corrected-weighting comparison: `data/ml/recency_training_mixed_20261014/`; `readiness.json` is active after final independent delta review; the controller is `dbrel-recency-mixed-controller-20261014.service`, with progress in `controller.log` and `queue.json`. `date-exposure-audit.json` records the bounded date-only mixing audit and its RNG limitation.
- Preserve the user's existing constraints: work stays in the repository, no commits or model deployment, bounded memory with job swap disabled, and unchanged browser prediction contracts.
