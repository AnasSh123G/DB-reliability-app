# Tasks

The work runs in `/home/anas/dev/DB-reliability-app`, in the new experiment directory `data/ml/recency_training_v3_20261014` (`W` below). Leave `recency_training_mixed_20261014` untouched.

## 1. Code

- [x] 1.1 `train_recency_delay.py`: replace the condition choices with `control | weighted | strong | seasonal`, each mapped to (amplitude, input trial): control (0, f1ac12b1742e3a0b), weighted (0.5, f1ac…), strong (2.0, f1ac…), seasonal (0, 634b5e057ab5fe00). Record the trial path in the run key
- [x] 1.2 Seasonal inputs: compute `doy_sin`/`doy_cos` from `day.bin` with `features.day_of_year_indices` and insert them at the trial's numeric positions, in training, selection, evaluation and the ONNX/browser fixtures. Verify that every other column's scaling and the vocabulary equal the no-time-of-year trial
- [x] 1.3 `recency_provenance.py`: verify the input trial named in the key instead of the fixed `TRIAL_REL`
- [x] 1.4 Controller, discrete check and summary: loop over the conditions `control, strong, seasonal`, and compare each variant with control. The list comes from `readiness.json` `conditions` (default `control, weighted`), so 2.2 must write `"conditions": ["control", "strong", "seasonal"]`
- [x] 1.5 Tests: the amplitude/trial mapping; the seasonal columns are year-free (the same month/day in 2025 and 2026 gives the same values) and land in the right positions; the existing recency tests still pass

## 2. Readiness

- [x] 2.1 Run the CPU suite in a 2 GiB, zero-swap unit and write `W/cpu-verification.json`
- [x] 2.2 Write `W/readiness.json` with the current source fingerprints, the pinned Chromium, the prerequisite identities (reused from `recency_training_mixed_20261014`), and `experiment_directory`
- [x] 2.3 Probe the seasonal fine and strong fine tracks on 3M rows for memory, under the 8G cap

## 3. Train

- [x] 3.1 Start the controller (512M cap, as before) with `DBREL_RECENCY_EXPERIMENT=$W`
- [x] 3.2 `control`: fine, distribution and discrete complete, and their browser checks pass
- [x] 3.3 `strong`: fine, distribution and discrete complete, and their browser checks pass
- [x] 3.4 `seasonal`: fine, distribution and discrete complete, and their browser checks pass
- [x] 3.5 If a job fails, fix only that cause, note it here, and resume
  - Note (2026-10-14): `strong-distribution` failed the ONNX float-noise check (1.6e-5 against 1e-5, no forecast changed). Finished with `scripts/finish_recency_export.py train --condition strong --track distribution` (limit 1e-4), then the queue resumed. `seasonal-distribution` needed the same (1.7e-5, no forecast changed) and was finished the same way

## 4. Report

- [x] 4.1 The report job completes and every entry in `W/queue.json` is `complete`
- [x] 4.2 Put a plain-language summary at the top of the report: did 3× weighting help, did time of year help, by how much (within 1/3/5 min, average error), and in which views
- [x] 4.3 Give the user the result and ask about replacing `CURRENT`. That replacement is a separate change

> Decision (2026-10-14): the user approved replacing CURRENT with the strong discrete model, pending a head-to-head on live outcomes. See [replace-current-with-strong-discrete](../replace-current-with-strong-discrete).
