---
kind: spec
title: "Audit early-arrival labels, compare September with August, and test recency and cause features"
---

# Delay-model investigation and training specification

Execution status and remaining work are tracked in [the canonical open-task list](delay-model-open-tasks.md). The objective remains improved model performance; ingestion and label correction are supporting prerequisites.

## Objective and boundaries

Determine whether unusual early-arrival labels, changing route conditions, or missing construction/disruption information explain the errors before testing recency weighting. Keep the app’s endpoints, forecast fields and browser inference contract intact. Do not commit, change the production model pointer, modify source facts, or silently remove outliers.

All task scripts, caches, temporary files, reports, checkpoints and candidate artifacts must remain under `/home/anas/dev/DB-reliability-app`. Existing read-only data references may be read; do not create new external task outputs. Run memory-bounded jobs serially. This document specifies future work; no audit results, cause attribution or training improvements are claimed here.

The user requested statistics before retraining. The [miss report](delay-miss-diagnostics.md) supplies the first checkpoint. Next produce the label audit, September/August comparison and cause-availability report, and show those results before starting the fitting phase. Changes to labels or feature availability rules require an explicit, reviewable decision.

## Findings that motivate the work

The training-only baseline on September 6–19, 2026 misses by more than five minutes on 12.41% of displayed stop-arrivals. September 15 reaches 15.93%; several Munich stops spike together. Regional services produce 61.11% of misses; 83.45% of misses underestimate delay. The ten stations with most misses account for only 3.79% of all misses, so a handful of station corrections cannot explain the full problem.

There are 43,415 targets earlier than −5 minutes. Because the app displays nonnegative delay, those targets necessarily count as misses. Several RE 161xx services have roughly 250 observations each, nearly universal overestimation and average absolute errors around an hour. This motivates checking **labels and their source timestamps**, not assuming the model itself predicts extremely early arrivals.

Construction is a plausible explanation for a local change, not a confirmed cause. Recency can help if that change was already observable in the training window. If it first appeared in September, weighting August more heavily cannot teach the model that new event.

## Existing implementation and data

| Component | Present behavior | Implication |
| --- | --- | --- |
| `trip` | One recurring `train_id` per `service_date`, with path/feed version and run identity | A dated `trip.id` cannot match August to September directly |
| `trip_stop` | Scheduled/actual UTC arrivals, arrival delay seconds, sequence, station, quality and observation timestamps | Supports label reconstruction and matching corresponding stops |
| `train` | Source/identity, route path, train number, origin/destination and departure time | Supports auditing identity changes; a display name alone is insufficient |
| `tt_message` | Stop-linked cause/note type, numeric code, category, source `ts`, validity range and deletion versions | Causes exist, but historical availability requires additional proof |
| `notice_rate_daily` | Per-date station/line/train counts of construction/disruption notices | Supports retrospective coverage and historical aggregate features |
| `src/dbrel/ml/notice_rates.py` | Six historical works/disruption rates across station/line/train, plus missing indicators | Current model already uses broad historical cause information |
| Existing notice folds | Training excludes the row’s calendar month from notice counts; evaluation uses training-date totals | Preserve this behavior for the initial recency comparison |
| `scripts/train_compact_delay.py` | Streamed training, checkpoint recovery, chronological splits and export gates | Extend in a new experiment version; do not mutate completed experiment provenance |

`tt_message.ts` is parsed from the source message timestamp. The stored message row has no first-observed/fetched-at field or poll foreign key. `tt_poll.fetched_at` exists separately. A timestamp saying a notice began earlier does **not** prove the app had received it then. Check whether retained poll payloads can reconstruct first observation; do not assume a poll near a message’s timestamp is evidence of receipt.

The existing notice builder detects construction/disruption categories on non-deleted type `h` messages. Other cause types/codes must be inventoried from actual data and parser semantics before defining new families. Do not invent numeric-code meanings.

## Step 1 — Audit suspicious early-arrival targets

1. Freeze source database identity, prepared-data identities, label/feature code, existing predictions and ordered stop IDs. Write an immutable run manifest for this investigation.
2. Count targets below −5, −15, −30 and −60 minutes by service date, source, region, recurring train, route/feed version, station and stop sequence. Report denominators, quality flags and within-trip correlation. Keep raw and displayed prediction errors separate.
3. Inspect all recurring services with supported near-universal failures, beginning with the RE 161xx examples. Select reproducible examples across dates, stops and source/quality groups, plus matched normal controls. Retain original IDs and UTC timestamps in the audit table.
4. Reconstruct `(actual_arrival_utc − supposed_arrival_utc) / 60` and compare it with `arr_delay_s / 60` and the prepared target. Check schedule snapshots/version selection, route identity, service-day rollover, UTC/local conversion, terminal/origin handling, duplicate/repeated stations, observation freshness and matching of source runs. The current suspicious dates are not themselves evidence of a daylight-saving transition.
5. Classify examples as consistent genuine early arrivals, confirmed derivation/matching errors, insufficient evidence, or another documented case. Distinguish genuine negative labels from the display clamp’s inability to represent them.
6. Publish counts, examples and the estimated contribution to MAE/within-five misses. Propose a correction only where supported by source evidence. Never “fix” labels because the model missed them or because they are outliers.

**Gate:** show the audit findings. If a correction is accepted, version the label policy, regenerate affected prepared data and score all unchanged candidate checkpoints against the same corrected labels. Separate the benefit of a data correction from the benefit of retraining or recency. Unresolved labels remain present, with separately reported sensitivity metrics; do not silently exclude them.

## Step 2 — Compare September 15 with August, per recurring trip

### Comparison definition

The sole reference is the average over the **previous calendar month: August 1–31, 2026**, for each matching recurring trip and stop. Do not add an August 1–22 or weekday-only reference. This is a descriptive comparison, not a training feature: August 23–31 overlaps the existing selection split.

- Match September 15 runs first by recurring `train_id` and source, then verify path/direction and corresponding station occurrences. Match stops by station plus occurrence/compatible sequence and route; do not join only on station if a route visits it twice.
- Audit path, feed-version and scheduled-time changes. Report exact stable matches separately from verified cross-identity matches. Use route/source/run identity to investigate changed IDs; do not automatically merge equal display labels or line names. Report unmatched services explicitly.
- For each matched stop, compute August mean actual arrival delay, sample standard deviation, median, p90, observed-date count and missing/cancellation coverage. Compute `September15 actual delay − August mean delay` in minutes. Keep signed early labels visible.
- For each recurring trip, compute a same-stop mean delay for each August run and the September 15 run, then average August run means with **one vote per service date**. Prefer the fixed common stop set; require at least 80% of that set observed for a run and report exclusions. Also report final-stop delay separately where available. Do not let a longer route or a day with more observed stops dominate the trip average.
- Mark comparisons with fewer than 10 August dates as low support. Use all eligible matching August dates for the average; record weekday/service-pattern differences as limitations without introducing another baseline.
- Provide both the difference in minutes and, where August variance and support permit, deviation relative to August variability. A single September observation does not justify a precise trip-level confidence interval. Use paired trip/date cluster resampling for aggregated summaries; do not treat correlated stops as independent samples.

### Outputs and interpretation

Deliver a per-trip CSV, per-stop CSV, support/unmatched table and readable charts of deviations by route, region and service type. Include Munich September 15 hotspot services and network-wide controls; do not select only the largest positive deviations. Show the share of supported trips exceeding their August mean by 5, 15 and 30 minutes and the distribution of signed deviations.

Extend the daily trend to nearby September dates using the same matched sets to distinguish a one-day spike from sustained change. Compare observed delays as well as baseline prediction errors. A higher actual delay is different from a higher model error.

Cancellation is not an arrival-delay label. Report its coverage/rate separately rather than replacing cancelled or unobserved arrivals with zero or a made-up delay.

## Step 3 — Assess construction and other delay causes

### Retrospective explanation

Inventory `tt_message` type/code/category coverage on matched August and September trips, including missing timestamps/categories, duplicates, deletion versions and validity ranges. Deduplicate message versions and avoid multiplying arrivals by a one-to-many message join. Report message coverage by month, source and region: sparse collection can masquerade as fewer disruptions.

Attach supported cause families to September 15 matched trips and compare their deviations with matched trips without recorded causes. Distinguish “no recorded cause” from “verified no active notice.” Construction messages with relevant validity ranges support an association; they do not prove that works caused the observed delays. If timestamps/coverage are insufficient, report the construction hypothesis as unresolved.

Show whether notice prevalence and delay levels rise before September 15, and whether the same route/station groups are affected. Separate stop-specific notes from trip-wide notes and define propagation scope explicitly; do not broadcast a note nationwide based on a shared line label.

### Availability gate for predictive features

The forecast replay cutoff is the existing **scheduled arrival minus five hours**. A new direct cause feature is eligible only if the message version was demonstrably received by that cutoff, was active for the relevant trip/stop, and its deletion/version state is reconstructed as of that cutoff. Apply identical rules in training, validation and browser/live feature preparation. Missing or unprovable first-observation time means unavailable, not a negative construction flag.

Inspect retained raw poll content and message provenance. If past receipt time cannot be reconstructed, keep direct same-trip causes **retrospective only** and specify future ingestion of first/last observation timestamps and version state. Do not use source `ts <= cutoff` alone as a substitute for receipt evidence. Sparse online availability can also make a theoretically valid feature impractical; measure that coverage before fitting.

Prefer compact structured cause families, known/unknown/availability indicators and causal recent-history aggregates over a text model. No new text encoder or large embedding model is required. Candidate alternatives, contingent on evidence:

| Option | Benefit | Condition |
| --- | --- | --- |
| Recency weights with existing features | Isolates the effect of recent examples | First training comparison; keep notice features unchanged |
| Recent station/route cause rates | May capture changing construction exposure | Strictly past, available records; explicit smoothing and lookback; no row’s own eventual cause |
| Active planned-works/cause indicators | Can represent the specific event | Verified availability at five-hour cutoff and equivalent serving lookup |
| Direct eventual delay reason | Useful for explanation | Never a predictor when recorded after cutoff |

New cause inputs require an input/manifest version and shared feature preparation. Preserve external endpoints and forecast fields; use the exported model’s feature metadata for internal tensor layout. If the browser’s current data path cannot supply the inputs without changing the app contract, defer the direct feature and retain historical aggregates.

## Step 4 — Implement the recency experiment

Let `T` be the latest **training** service date and `S` the same day one calendar month earlier, clamped to that month’s final day if needed. For a training trip with service date `d`:

```
w(d) = 1                                      when d <= S
w(d) = 1 + 0.5 * (d - S) / (T - S)            when S < d <= T
loss = sum(w_i * per_arrival_loss_i) / sum(w_i)
```

Every arrival belonging to the same trip receives the same date weight. Preserve the existing per-arrival sampling unit; do not also reweight by route length or oversample recent rows. For the current frozen prepared data, `T = 2026-08-21` and `S = 2026-07-21`; August 22 is the requested split boundary but contains no eligible training rows. Evaluation stays unweighted. No validation/test dates enter weights, normalization statistics, vocabulary fitting or notice-history fitting.

Expose per-example losses for quantile and classifier objectives before applying weights. Preserve all loss components and coefficients. Verify that uniform weights reproduce the original scalar reduction and that weights remain aligned after block shuffling. Log true weighted numerator/denominator across the epoch, not an unweighted average of batch means. Record the calendar window, amplitude, date source, data/code fingerprints and weighted row totals in resumable checkpoint keys and exported manifests. Refuse resumes across changed weighting/data/feature policies.

Initial recency-only experiments keep current loss families, architecture, feature preparation, optimization settings, seed and epoch budget fixed. Do not simultaneously change notice folds or tuning parameters: that would confound the comparison. A subsequent cause-feature ablation is contingent on Step 3 and uses the same three candidate families, not additional model architectures.

## Step 5 — Fit and test only the three selected candidates

| Candidate | Training | Output evaluation |
| --- | --- | --- |
| Fine-tuned MLP | One recency-weighted quantile-network fit | Existing ordered p25/p50/p75 output |
| Discrete-quantile classifier | One recency-weighted classifier fit | Existing CDF quantile policy and tolerance |
| ±3-minute-window classifier | Reuse the same classifier fit | Existing window decision rule |

Thus three candidates require **two underlying fits**. Exclude the wider MLP, trees and other architectures. Keep each classifier decision rule explicit; selecting a checkpoint for one rule is not automatically optimal for the other. Evaluate the three outputs on the existing selection split with their frozen selection criteria, reuse stored epoch checkpoints where needed, and keep the later comparison split out of checkpoint selection. Count both classifier decision views as candidates in the reporting rather than presenting one as a free independent experiment.

Use the original fit with actual eligible rows through August 21 (requested boundary August 22), selection August 23–September 5 and later comparison September 6–19 to measure recency fairly against existing unweighted results. These dates are already explored; label the results as reused validation, not fresh proof. Do not repeatedly inspect September 20–26 to choose weights/features: it has already been examined in earlier work and is not untouched.

To test whether adaptation helps an event that starts in September, specify a separate chronological replay only after the descriptive comparison: e.g. fit through September 14 and forecast September 15. Choose the epoch budget on an earlier disjoint chronological training/selection pair (for example, fit through August 22 and select on August 23–September 5), freeze that budget and decision rules, then refit through September 14 without selecting checkpoints on September 15. Refit all preparation/notice counts on the replay training dates. Compare weighted and unweighted controls on identical replay data, frozen budgets and settings; the original August-trained model is not an adequate control for a longer training history. This adds fitting cost, so it is a separately reported, conditional experiment restricted to the same three candidates, not an automatic expansion of the initial two-fit budget. September 15 is a hypothesis-informed diagnostic replay, not an independent generalization test. Future unseen complete dates are needed for that claim.

## Step 6 — Verification and decision report

- Test calendar-month boundaries, weight endpoints, same-trip weights, shuffle alignment, weighted loss reduction and interruption/resume identity. Keep tests small and CPU-only.
- Write ordered stop-ID companions and hashes for **every** candidate prediction array. Verify alignment to prepared rows before station/trip attribution. The older candidate spatial CSVs remain unverified and must not be reused as certified attribution.
- Report raw and app-displayed within 1/3/5 minutes, MAE, quantile ordering/calibration, coverage and missing predictions. Include early-label sensitivity, service/date/state/route breakdowns and the matched September/August cohorts. Compare equal eligible rows.
- Report paired improvements clustered by trip/service date, supported subgroup effects and tradeoffs; three views sharing classifier weights are not independent models. Keep poor-tail performance visible even if overall accuracy rises.
- Measure complete manifest-inclusive download size, targeting at most 35 MB; classify any flexible-budget exception explicitly. Verify ONNX/native agreement, then actual pinned browser runtime single/batch calls including UNKNOWN categories, unchanged shape and output semantics. Include representative new cause availability/missing cases if those inputs are introduced.
- Require every feature and metadata dependency needed for browser inference in the candidate artifact or existing serving path. Record size, latency and memory. Candidate improvements do not authorize deploying or committing them.
- Choose whether recency improves supported September shifts without unacceptable precision/tail regressions. If it fails, report that result rather than increasing weights until the reused validation looks better. Cause-feature changes receive their own comparison against the recency-only result so the source of improvement is clear.

## Resource limits and deliverables

Use private cgroups with swap disabled: diagnostics at most 2 GiB, report generation 1 GiB, fitting at most 8 GiB with a 5 GiB high-water target. Check available host RAM before each job and leave at least 5 GiB headroom beyond the anticipated job peak; wait or reduce the workload when that cannot be met. Stream/memmap data, batch SQLite lookups and aggregate one day/block at a time. Do not perform full national message/arrival joins in memory. Log memory peaks and recover from a job-limit termination using the last committed checkpoint.

Deliver these files under a new versioned `data/ml/` experiment directory, with readable reports under `docs/`:

1. Frozen identities and ordered row manifests.
2. Early-label audit tables/examples and proposed corrections or unresolved cases.
3. September 15 versus previous-calendar-month-only per-trip/per-stop means and deviations, support/coverage and charts.
4. Cause coverage, construction association and cutoff-availability audit.
5. Recency policy, CPU verification and two-fit/three-candidate results.
6. Browser parity, full artifact sizes, resource measurements and a recommendation with limitations.

**Open evidence questions:** Are the extreme early labels genuine? Can first receipt of historic messages be reconstructed? Did construction exposure change before or only after the original fit cutoff? How many recurring services match across schedule versions? These are investigation outputs, not settled assumptions.


## Execution checkpoint — October 13, 2026

The previous-month-only comparison and source/cause investigation are in [the investigation report](delay-shift-investigation.md). The initial export failure was recovered from saved matrices/CSVs without recomputing the full comparison; missing daily cause counters remain missing. Publication identities and normal controls were subsequently verified.

Raw plans reveal a concrete schedule discrepancy in all 282 inspected negative RE 161xx stops on September 15: stored actual times agree with raw changes, but stored schedules disagree with raw plans. Shared pattern times are pinned at first observation in the ingestion code. The proposed correction is dated trip-specific planned-time snapshots with source-recording provenance, consistent training/evaluation regeneration, and a separate measurement of correction effects. The user subsequently approved this correction policy; the three-date pilot below validates the new target-only version before expansion.

Recency code and seven CPU checks are prepared under new filenames; no full-history fits, direct cause feature changes, database corrections or commits have occurred. Existing source facts, prepared caches and production pointer remain unchanged.

The current historical notice folds can use later *other-month* training records for an earlier training row (for example, July rows using August records). The recency-only comparison must disclose this existing retrospective assumption. A new recent-cause feature requires its own strictly-past availability audit.


## Approved correction policy and next comparison

The user approved dated schedule/label correction before recency fitting. Build a new local prepared-data version over the original frozen stop IDs and dates. Correct a target only when an exact source run/station/sequence has a unique raw API-plan timestamp and a raw changed timestamp matching the stored actual time, demonstrably recorded at least five minutes after that arrival. Require declared archive coverage for the full target and following dates and the preceding date’s hours 20–23; incomplete coverage retains all original labels for that date with status 5. Preserve original labels where support is missing or ambiguous and flag them explicitly. Preserve original database facts and caches. Retain corrected finite values outside the prior two-hour bound with explicit counts; do not silently clip or remove rows from the controlled comparison.

The new version changes retrospective target-reference schedules. Existing encoded forecast inputs are retained for the controlled experiment: late-observed plans do not replace historical input features. This keeps model input contracts and the existing timetable-as-of limitation explicit. Future trip-specific serving/feature schedules need independently verified availability and cannot be inferred from target corrections.

Fit unweighted and recency-weighted controls on this same corrected version, using only the three selected candidate families. One classifier supplies both discrete-quantile and window outputs per condition: four underlying fits total, rather than six. Score unchanged original checkpoints on the corrected targets first to separate relabelling effects from refitting. Report verified-label and unresolved-label subsets, raw/displayed metrics, and shared-classifier checkpoint policies. Fetch missing original raw partitions into the project with resumable downloads and disk/RAM reserves.


### Observed training-date boundary

The original split key requests dates through August 22, but the finalized-before filter leaves **no prepared training arrivals on August 22**. The actual latest training service date is **August 21, 2026**. Anchor the approved recency ramp to actual prepared trip dates: July 21–August 21, reaching 1.5 on August 21. Record the requested split boundary separately from the observed data boundary. The pilot consequently checks November 3, August 21 and September 15. The August comparison remains August 1–31 only.


### Three-date pilot acceptance

The version-2 pilot processed all three required dates: November 3, August 21 and September 15. September 15 has 336,511 source-verified arrivals and 30,909 corrected targets, including 2,763 targets changing from more than five minutes early to nonnegative delay, plus 100 changing into the −5 to 0 minute range. The producer’s legacy counter named `corrected_negative_to_nonnegative` actually measures `<−5` to `≥−5`; the separate pilot threshold summary records the precise counts. August 21 has 172,832 source-verified arrivals and 15,787 corrections. November 3 has missing declared coverage for the preceding date’s hours 20–23; all 349,257 original targets are retained and flagged unresolved. No corrected target in this pilot used a raw plan as a forecast-time feature. Completion of the full version remains required before fitting.


### Controlled fitting and verification queue

The full correction version is running serially under a 2 GiB cap. `verify_correction_version.py` verifies all committed label/status/schedule blocks and evidence hashes before use. `score_label_correction_baselines.py` replays unchanged original exports on the same old and corrected targets; it does not rely on the older unbound candidate arrays. Only after this measurement does `run_recency_training.py` permit corrected-data controls and weighted fits: fine MLP plus shared classifier per condition, four underlying fits producing three outputs per condition. Classifier views choose their own epochs using selection dates only; saved epoch hashes bind exported weights to training metrics.

The queue requires a source-fingerprinted readiness record after CPU tests and independent review. It caps full fits at 8 GiB (5 GiB high-water target), other stages at 2 GiB, disables job swap and leaves five GiB of host memory beyond each job cap. Any failed stage stops later stages and preserves checkpoints. Browser checks for all six artifacts and a report with raw/displayed and verified/unresolved metrics are mandatory completion gates. Full correction and model comparison results are pending; the source database and deployed model remain unchanged.


### Corrected previous-month comparison

Before fitting, `compare_corrected_august.py` overlays only source-verified corrected targets onto the original matched August 1–31 and September 6–19 matrix. It keeps the same recurring services, missing values, fixed common stops, 80% run-coverage threshold and one vote per August date. It publishes new trip/stop tables and per-date correction coverage under `data/ml/recency_training_20261013/corrected_august_comparison`; the original comparison is preserved. August 22 and other source rows outside the eligible corrected cache retain original labels and are explicitly unverified. This is a partially verified target overlay, not a claim that all August arrivals have been corrected.

Sixteen CPU tests passed after this stage was added. The full correction producer continues unchanged under its 2 GiB cap. The reporting consumer remains under integration review; no readiness marker has been issued and no recency fit has begun.


### Rate-limit recovery and automatic continuation

The first full pass stopped on an archive HTTP 429 after 83 minutes; committed dates and corrected blocks were preserved. The resumed service uses `resume_schedule_corrections.py`, a transport-only wrapper around the unchanged producer/parser. It records its own transport-policy identity, waits five minutes initially, paces downloads, retries only transient transport errors with bounded exponential delay, honors Retry-After, and saves each successful download to the local archive manifest. It does not change correction rules. Three synthetic transport tests passed; the combined CPU suite now has 35 passing tests.

`await_recency_readiness.py` runs under a 512 MiB, zero-swap supervisor cap. It waits for a complete correction version, a final reviewed source-fingerprint readiness marker, and the correction service to stop before running the serial comparison. The controller requires pinned ONNX Runtime Web 1.30.0, refuses changed reviewed sources or concurrent/residual fit services, and stops later stages on errors or insufficient RAM. Final source review is pending; readiness has not been issued and no new fits have started.


## Permanent ingestion fix — authorized October 14

The user authorized fixing future ingestion while the offline correction runs. The evidence-backed scope covers live/archive plan receipt metadata, `upsert_run`, recorded-change polling and finalization. Keep recurring `StationStop` patterns for route identity and static GTFS compatibility; add append-only dated-trip/stop schedule evidence containing absolute planned UTC times, source response hash and recorded receipt time. Exact replays are idempotent. Preserve revisions and original observations; ambiguous planned times have no confirmed delay, and a missing TT dated schedule never falls back to an older recurring pattern. Use the same resolver in polling and gap finalization, retaining station/path/sequence checks and never turning missing observations into punctual arrivals.

Tests first reproduced both paths: day one scheduled12:00/actual12:10 and day two scheduled11:00/actual11:10 on the same train/path/stop. Both must be ten minutes late, and importing day two must not change day one. Additional checks cover receipt/as-of identity, repeated imports, conflicts, midnight/route variants, GTFS compatibility and archive replay through the same ingest functions. The additive table is registered in metadata; no existing table or source fact is dropped. The running correction database, parser/archive dependencies and source content identities remain frozen. Migration activation and any historical re-ingestion must wait until the frozen-data model comparison no longer depends on the current database bytes. Existing APIs and browser forecast output contracts are preserved. Rollback is to stop new ingestion and retain the append-only evidence; reverting to the stale TT fallback would reintroduce invalid labels.

### Implementation verification and activation

The implementation stores `TripScheduleSnapshot` evidence through live `/plan`, full `/fchg` and archive replay. Replay retains earlier same-bucket plans even if the latest XML is malformed. Polling resolves plans received by that response's own timestamp; finalization uses its explicit cutoff. When a later receipt conflicts with an already recorded reference, reconciliation clears the ambiguous side's derived delay and updates delay rollups while preserving actual times, cancellation information and actual-observation receipt times. Snapshot receipts remain the separate evidence of when each planned reference became available.

A station-level full update never replaces a known trip's route or endpoints. Partial plan imports recover the tracked extent from all dated evidence for that trip/path; journey closure bounds cover the earliest first departure and latest possible final arrival. These bounds do not certify an ambiguous stop delay. Overlapping plan receipts undergo the same existing sequence-based daylight-saving disambiguation using copies, preserving the parser's original objects and source response hashes. Explicit cancellation deltas are retained without inventing actual times or zero delays.

The bounded verification run passed **232 tests**, including the CLI migration, live response completion timestamps, polling, archived replay, finalization, conflicts, receipt cutoffs, cancellations, planned and actual DST cases, and receipt insertion across batch boundaries. Combined statement/branch coverage across the touched ingestion modules is **87%**. Peak test memory was reported as **114.6 MB**, under a 2 GiB private cap with swap disabled. Exact source fingerprints and coverage are saved in `data/ml/dated_schedule_ingest_20261014/verification.json` and `coverage.json`. Independent implementation review has passed; these results do not certify a national replay or a real-database migration.

The final review identified an actual-time ambiguity during the autumn repeated hour. The future-ingestion path now abstains on actual sides with two valid UTC interpretations, preserving a clear other side and explicit cancellations. Scheduled proximity and response time alone do not choose a fold. This prevents both a fabricated early arrival and treating a possible second-occurrence forecast as an actual during the first occurrence. The frozen parser and correction inputs are unchanged. Append deduplication now uses the snapshot's unique index with bounded insert batches instead of loading the trip's whole receipt history. The [independent review addendum](dated-schedule-ingest-review.md) resolves both findings: 43 public contracts and a separately bounded 999-parameter insertion probe passed. Production-scale performance, another SQLite runtime and real-database activation remain unverified.

After the corrected-data comparison and final report finish using the frozen source database:

1. Pause collection and keep the existing database backup/recovery procedure.
2. Run `.venv/bin/dbrel migrate-trip-schedule-schema`. This creates only the new evidence table on an existing collection, leaves existing facts unchanged, and validates an already-present table rather than accepting an incompatible partial schema. Ingestion fails with a migration instruction if the table is absent.
3. Resume collection with the updated code. Newly captured dated plans and full change records supply the references for future observations. Migrating the table alone does **not** recover schedules for historical trips; historical replay requires the original responses and a separately scoped ingestion operation.
4. Monitor missing/conflicting plan evidence and retained cancellations. Keep unresolved references unknown rather than restoring the recurring TT-time fallback.

No migration or collection has been started against `data/dbrel.sqlite`. The running correction's producer/parser/archive files and the deployed model pointer remain unchanged. This patch fixes ingestion of delay references; it does not establish forecast-cutoff availability for all historical messages or change the browser predictor's existing timetable feature assumptions.

### Browser identity gate for the pending comparison

The corrected-data comparison now uses `check_recency_browser.py` with an explicit Chromium binary/hash pin. It verifies the binary before and after inference and records path, SHA-256 and version. The controller and final report require this exact readiness identity and ONNX Runtime Web 1.30.0. The prior frozen browser checker remains unchanged. The updated CPU comparison suite passed 40 tests under a 2 GiB zero-swap cap. The independent browser/controller/report review passed, and source-fingerprinted comparison readiness is now saved. The supervisor still requires complete correction and service exit before executing the comparison. Future-ingestion activation is outside this readiness scope.


### Execution checkpoint — October 14, 05:05 Europe/Berlin

The full correction completed all 320 dates. Verification accepted the corrected columns, evidence and source identities before both pre-fit stages ran. The [coverage and unchanged-model baseline report](correction-baseline-results.md) records 5,360,179 changed labels, 67.06% source-verified training rows and 99.52% source-verified later evaluation rows. Unsupported labels remain retained and flagged.

The corrected August-only comparison is complete: across 24,252 supported recurring trips, September 15 averages +0.63 minutes versus August, with 10.71% more than five minutes worse. This does not identify a construction cause. The unchanged current model scores 84.74% within five minutes against corrected later targets; this is the reference for subsequent improvement claims.

The corrected-data unweighted fine MLP fit completed with 87.50% later within-five accuracy and 2.617-minute raw MAE. Native/ONNX parity passed and the complete download is 15.86 MB; actual-browser checks remain pending. The unweighted classifier is active. Weighted fits, six browser checks, paired/subgroup analysis and the final recommendation remain open in the [canonical task tracker](delay-model-open-tasks.md). This checkpoint supersedes earlier pending/running statements above; it does not authorize deployment or database activation.


### Recency implementation correction — fixed normalization and mixed dates

The first comparison exposed a batch-layout defect: dividing each date-local mini-batch by its own weight sum cancels nearly uniform trip-date multipliers. This is an implementation bug, not evidence against recency weighting. Preserve the first attempt as diagnosed evidence; do not promote its recency interpretation.

The approved intent is unchanged: old arrivals have weight 1, with a linear ramp to 1.5 on August 21 for July 21–August 21 trips. Implement a fixed full-training mean-weight normalization rather than a batch-dependent denominator. Mix independently shuffled date blocks in bounded pools before creating mini-batches, using exactly the same sampler, row coverage, seeds and budgets for controls and weighted fits. Every training row remains visited once per epoch; checkpoints bind the sampler, mean-weight normalization and source identities. Add a regression proving homogeneous recent batches retain their gradient multiplier, plus alignment, exact coverage, deterministic resume and memory probes. Independent review must assess optimizer/batching interaction before restarting the four underlying fits. Keep the original corrected data, three output types, 8 GiB/no-swap limits and browser contracts.


### Corrected-weighting readiness checkpoint

The corrected implementation uses eight independently permuted contiguous read spans per pool (at most 2,097,152 rows), followed by within-pool mini-batch shuffling. Every row is visited exactly once per epoch. A fixed full-training mean weight of approximately 1.02521 preserves the requested old/new ratio of 1–1.5; normalization scales both consistently. Controls use the identical mixing scheme and a mean weight of 1. Scheduler preflight requires the read-span size to divide the mini-batch size, containing random partial-span placement; the frozen 262,144 / 8,192 sizes satisfy this requirement.

The new experiment is isolated in `data/ml/recency_training_mixed_20261014`, selected through a repository-guarded environment setting explicitly passed to children. First-attempt outputs and source snapshots remain intact. The controller can reuse completed baseline/August measurements only after verifying readiness-pinned prerequisite file content identities; the final report still recomputes and verifies their saved cohort statistics. Updated sampler metadata and normalization are required for paired reporting.

Sixty-two CPU tests passed, including both heads/member counts for aggregate gradient equivalence, a direction-changing old/recent contrast, actual trainer gradient interception, exact row coverage and deterministic interruption/resume. Two complete-pool probes on each architecture passed under 8 GiB with zero swap, peaking about 2.3/1.9 GiB. A separate full-date audit confirms 8,749,367 recent rows and substantial mixed-date mini-batch exposure; it uses a representative NumPy row permutation and does not claim exact replay of Torch/dropout randomness. Final independent delta review remains required before launching four full fits and six browser/report gates. These tests establish implementation readiness, not a model improvement.


### Corrected comparison launch — October 14

Independent delta review passed for the current implementation hashes; see [recency-weighting-normalization-review.md](recency-weighting-normalization-review.md). The 62-test source snapshot, two successful full-pool probes, fixed-normalization/mixing metadata, exact Chromium identity and 39 reused prerequisite content identities are bound by active readiness under `data/ml/recency_training_mixed_20261014`.

The serial controller `dbrel-recency-mixed-controller-20261014.service` has launched. It verifies the preserved completed correction/baseline/August inputs before four matched fits, discrete interpretation, all six browser checks and the final report. Parent cap 512 MiB, child fit cap 8 GiB/high 5 GiB, other stages 2 GiB, all zero swap; host headroom checks remain enforced. Do not edit fingerprinted experiment or correction sources during this queue. Source database activation, model deployment and commits remain deferred.
