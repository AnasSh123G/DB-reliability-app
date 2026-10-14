---
kind: review
title: "Recency training scripts: independent review"
---

Cold source review of the recency training code, correction baseline and August comparison, controller, report consumer, archive retry wrapper, and readiness supervisor, against the frozen runner/checker, shared model code, artifact writer, current spec, and current correction/recency policy. No training, dataset, GPU, job, or browser execution was performed. The v2 pilot outcomes below are taken from the parent-provided aggregate report, not independently recomputed.

## Current findings

### P2 — Browser check does not identify or pin the Chromium runtime

**Location:** `scripts/explore_delay_browser.py:146–149, 182–186`; `scripts/summarize_recency_training.py:282–297, 319–322`.

The browser runner chooses `sorted(chromium-*/chrome)[-1]`, does not record the selected Chromium build, and sets `runtime_version` to the ONNX Runtime Web package version. The report accepts parity without checking either field and returns `browser_chromium` as null for results produced by this runner. The controller pins ONNX Runtime Web and fingerprints the frontend/runtime package metadata, but the claimed actual pinned browser runtime is not identified or pinned.

**Action:** launch a specifically pinned Chromium revision, record its revision/version (and preferably binary digest) in the result, and have the report reject missing or unexpected browser identity.

### P3 — Retry wrapper's policy marker is not atomic and its retry count is ambiguous

**Location:** `scripts/resume_schedule_corrections.py:17–18, 35–43, 48–51`.

`POLICY['attempts'] == 8` permits eight total calls, which is seven retries after the initial request, while the requested transport policy is described as eight retries. Also, `transport-policy.json` is written directly; interruption during the write can leave invalid JSON, and the next resume fails at `json.loads` before the producer starts.

**Action:** name the field `max_attempts` or configure nine attempts for eight retries, and write the policy record via temp file plus atomic replace.

## Earlier findings resolved in current source

- **Report database snapshot:** `summarize_recency_training.py` now binds group attribution to the frozen source DB SHA, rejects a nonempty WAL, and verifies file size/mtime and SQLite `data_version` around the read-only snapshot.
- **Report quantile diagnostics:** the report validates full-cohort producer quantile denominators and finite calibration rates, then publishes those metrics with an explicit note that point-only arrays cannot reconstruct interval coverage.
- **Corrected-August report gate and identity:** the final report now fails when the comparison result is missing and checks the result's producer script SHA before accepting it.
- **Readiness source coverage:** `run_recency_training.py` now fingerprints the previously omitted helper modules, model/preparation dependencies, and frozen correction producer/parser/archive sources.
- **Correction verifier source identity and copied features:** `verify_correction_version.py` binds `source-inputs.json` to the setup hash, checks original fit/eval metadata and columns against the manifest, and requires copied non-target `.bin` columns in the corrected cache to match the frozen original source hashes before checking corrected targets/status/schedule/evidence.
- **Discrete history identity:** `train_recency_delay.py:536–539` stores `history_sha256` in the completed training result, and `check_recency_discrete_delay.py:153–160` verifies it before ranking epochs, in addition to checking the selected checkpoint digest.
- **August overlay implementation and snapshot guard:** `compare_corrected_august.py` uses only August 1–31 for the reference, gives each accepted day one vote, requires 80% date coverage over a fixed set of common September stops, overlays only source-verified targets, retains uncovered originals with explicit coverage counts, and writes under the experiment directory. It now rejects a nonempty WAL before hashing/opening the source database and after its read transaction, and checks the database mtime against the hashed snapshot. The three synthetic tests cover month boundary, coverage threshold, missing-value handling, and insufficient coverage; they were not run in this review.
- **Controller preflight/readiness coverage:** `run_recency_training.py` fingerprints the report, browser runner, comparison, verifier, training, and relevant feature/correction sources before checking readiness. It runs the corrected-August comparison and unchanged-checkpoint baseline before the four controlled fits and later browser/report stages.
- **Controller serialization and runtime dependency:** the controller uses a nonblocking file lock, refuses to resume while an earlier bounded child unit remains active, fingerprints the frontend predictor and runtime package metadata, and checks the required ONNX Runtime Web version before launching jobs. `await_recency_readiness.py` waits for correction completion, review readiness, and the correction unit to stop before invoking the controller.
- **Completed discrete result idempotence:** `run_recency_training.py` no longer skips on result-file existence. The discrete checker recomputes its source key, verifies the selected checkpoint digest, and on a completed result checks prediction-array and export/artifact hashes before returning.
- **Correction source identity:** `verify_correction_version.py:20–39` binds `source-inputs.json` to the setup hash and checks original fit/eval metadata and columns against the manifest before validating corrected columns and committed per-date y/status/schedule/evidence blocks.

- **Fit/validation correction-policy identity:** `train_recency_delay.py:480–482` now compares policy, setup SHA, and progress SHA before training.
- **Selected epoch checkpoint identity:** `train_recency_delay.py:455–464` records the checkpoint SHA after each epoch save, and `check_recency_discrete_delay.py:156–160` verifies the selected digest before loading it; the tautological duplicate load is gone.

- **Shared binned fit / independent rule selection:** `train_recency_delay.py:442–447` logs window and discrete metrics for every epoch, and `check_recency_discrete_delay.py:153–160` selects the discrete epoch using its own frozen within-5 ranking on the selection split. The existing classifier fit is shared, while the discrete rule chooses its own epoch. The checker stores `selected_epoch` as an integer and `selected_metrics` separately.
- **Ordered prediction IDs:** `check_recency_discrete_delay.py:226–234` verifies the source ID hash against the completed result, checks shape and row-by-row alignment to the prepared comparison slice in bounded chunks, and only then copies IDs and writes the final identity.
- **Prepared-data content identity:** `train_recency_delay.py:316–321` hashes each prepared column along with size and mtime; `recency_provenance.py:27–29,53–56` documents and verifies all three before postprocessing. This closes the same-size/restored-mtime gap.
- **Correction/recency boundary:** the current spec anchors the ramp to the actual prepared Aug 21 end date (July 21–Aug 21) and requires a raw changed-time receipt at least 300 seconds after actual. It declares target/next-day plus prior-day 20–23 archive coverage; incomplete coverage retains original labels with status 5. The provided v2 pilot summary says Nov 3 correctly retained all original labels under status 5, while Aug 21 and Sep 15 had verified corrections. Training refuses incomplete correction status.
- **Weighted/unweighted loss control:** the code applies each row's weight using the same shuffled row index and normalizes the batch weighted sum by the weight sum. At amplitude zero, every weight is one and the objective reduces to the existing per-arrival mean loss; for the quantile and binned heads, `per_arrival_loss()` reduces the same per-arrival components as the existing loss, including the ensemble-member mean. The calendar-month ramp uses the last actual fit date and clamps the preceding month to its last valid day.
- **Existing artifact/promotion safeguards:** the scripts write offline candidate artifacts and do not update `CURRENT`. ONNX parity is checked by the scripts; browser-runtime parity remains in the deferred stage above.

## Review limits

This is a source review plus the parent-provided pilot aggregate summary. It does not establish full corrected-cache completion, fit outcomes, browser parity, or displayed-delay results. Those remain gates before interpretation or deployment claims.
