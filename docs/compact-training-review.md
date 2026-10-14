---
kind: review
title: "Compact training runner: cold review"
---

Reviewed on 2026-10-13 against [the training plan](compact-training-plan.md). Scope: `scripts/train_compact_delay.py`, `scripts/explore_delay_browser.py`, and `scripts/explore_compact_delay.py`, including their production network, loss, prepared-input, export and browser contracts. This review was independent of implementation; the reviewer edited only this document.

## Result

**No remaining actionable correctness findings in the final reviewed source snapshot.** The implementation agent addressed the findings below during review. This is a source review with small CPU helper checks; it does not certify full-fit resource peaks, GPU resume equivalence, or candidate browser performance.

## Issues corrected during review

The implementation agent made the following fixes after review feedback; they are no longer open findings in this snapshot:

| Issue | Current correction checked in source |
| --- | --- |
| Browser checker required a candidate named `binned` and only probability outputs | Candidate name comes from the fixture manifest; quantile and probability outputs are both handled |
| A failed ONNX parity check still produced a completed run | Failed numerical parity or size now raises and retains a failure report and checkpoint |
| Interrupted export prevented ordinary resume | Existing exports are retained under local timestamped names before retry |
| Replacing `best.pt` before committing `latest.pt` could separate weights from checkpoint metadata | `latest.pt` references the epoch-specific best weights; final `best.pt` is derived from that committed reference |
| Reference scores reported zero quantile coverage although no percentiles were available | Quantile diagnostics are null when no percentile rows were scored |
| Chromium's default temporary profile was outside the repository | Its temporary directory is explicitly created under the experiment directory |
| Reference predictions and imported loss/decision code were absent from the resume fingerprint | The key includes reference hash, precision constraint, production source hashes, and prepared-file size/mtime identity |
| CPU/CUDA execution could change under an unchanged resume key | The key now includes device and Torch/HIP/CUDA versions; the device CLI is restricted to CPU or CUDA |
| The experimental size gate counted ONNX plus incomplete `inputs.json` | `artifact_export()` writes a full offline arrival manifest, verifies training-only notice rates through a read-only SQLite URI, measures actual model-plus-manifest bytes, enforces 36 MB, and records the strict 35 MB classification |
| The single-call browser loop missed appended UNKNOWN-category fixtures | The 21-call sample now includes the final all-zero-category row, and the hardcoded 1,000-row timing label was removed |

## Verification and limits

- Read the frozen prepared keys and trial configuration: training covers 2025-11-03 through 2026-08-22 with 88,948,878 rows; validation covers 2026-08-23 through 2026-09-19 with 7,967,853 rows. Training notice inputs use leave-one-month-out folding; validation uses the same training notice fingerprint without folding. The warm-start path is the original search trial, not the deployed final refit.
- Read the epoch-selection and evaluation call sites. Epochs are ranked on the prefix before 2026-09-06. The later fortnight is evaluated after restoring the selected weights. The distribution's selection rule is ±3 minutes, with median/±3/±5 reported on the frozen later probe. No direct date leakage was found in this snapshot; both fortnights remain previously inspected validation data.
- Executed small, in-memory CPU checks extracted from the actual helpers: date-boundary behavior and ordering rejection; finite and ordered percentile validation; missing-coverage rejection; and precision-constrained ranking. They passed. All three scripts compiled without writing bytecode. No dependencies were installed, no training or GPU access occurred, and no running jobs were polled.
- The source uses copied blocks of 262,144 training rows, 8,192-row inference batches, and cgroup guards requiring at most 8 GiB RAM and zero job swap. The tensor-size guard is called before training. No full feature matrix is materialized in the new runner. This is source inspection, not a measurement of full-fit memory or GPU resume behavior; the launcher must still enforce the plan's one-job rule and available-memory/probe headroom.
- The reviewer did not run the separately authored test suite or a browser process. The prior reported HIP initialization failure is not evidence of invalid data and is not a review finding. `models/v2/CURRENT` was read as `20261004T213106Z`; this review made no implementation, model, database, or pointer writes.
- The manifest and fingerprint corrections were subsequently inspected in source. Their runtime checks and new candidate browser/Python parity remain the implementation agent's validation work; this review does not claim those runs passed.

## Reviewed snapshot

Files were changing during the review. The conclusions above describe these final SHA-256 snapshots, not subsequent changes:

```text
b464e22d93e2fcb51c0e55a22962f5b85ef8c6b9e86121b27ff7dfad29e78d2a  scripts/train_compact_delay.py
11084b8ce92cf13880b325a16670dfdff17a70b223fcea0311eee4d0c5c4237e  scripts/explore_delay_browser.py
00c6905842c79adb3467ffa2d0bb58e263d3cb1bd791de3314bb8930e958b8c5  scripts/explore_compact_delay.py
```
