---
kind: review
title: "Review: recency weighting normalization and bounded date mixing"
---

Reviewed October 14, 2026, against the working tree based on `2d6b28ff51885c9d16bf51026a41f08f1cdb260c`. This review owns only this document. The hashes below identify the inspected sources.

**Outcome: passed for the inspected source delta.** The fixed training-set mean denominator and shuffled eight-span pools are an appropriate remedy for the diagnosed weighting failure. The conditional scheduler defect found during this review is now contained by a divisibility guard and a regression fixture. No remaining P1/P2 correctness defect was found in the reviewed delta. Experiment isolation, prerequisite reuse, and the new paired sampler/report checks are sound within the stated frozen-input workflow. The parent reports the CPU and full-pool resource checks passed; empirical model improvement remains to be measured by the new comparison.

## Prioritized findings and resolutions

### P1 — The first attempt did not apply the intended recency weighting across date-local batches

**Status:** confirmed historical defect; the current loss formula addresses it. The first attempt's scores cannot establish whether the intended recency policy improves forecasts.

The earlier trainer divided each batch's weighted numerator by that batch's sum of weights. For a homogeneous batch with weight `c`, both the loss and its gradient reduce exactly to the ordinary unweighted batch mean. Shuffling date-local blocks and then their rows preserves this cancellation.

The supplied `weight-layout-audit.json` reports 88,948,878 rows, 340 original blocks, 309 constant-weight blocks, and 35 blocks containing recent rows. Its range 0.986254–1.015095 uses the **block mean as a proxy** for shuffled mini-batch normalization; it is not a measurement of every emitted mini-batch coefficient. Some within-block relative weighting and optimizer-history effects can remain. Exact cancellation in a constant-weight batch is sufficient to invalidate the claimed 1-to-1.5 temporal treatment.

The logged epoch metric `sum(weighted losses)/sum(weights)` did not prove the training updates used the corresponding full-fit objective. Retain the diagnosed attempt and its provenance, and rerun both conditions with the corrected sampler and denominator.

### P2 — Random placement of a short original span could invalidate OneCycle's update count

**Status:** resolved in `scripts/train_recency_delay.py:402`; regression in `tests/test_recency_mixing.py:340`.

Inherited `source.spans()` describes contiguous pools of `mix_blocks * read_block_rows`. The sampler instead groups randomly permuted original spans, so the short original span can appear in an earlier pool. With read-block size 4, mixing 2, 19 rows, and batch size 3, the contiguous prediction is pool lengths [8, 8, 3], giving 7 updates. A valid emitted arrangement [7, 8, 4] gives 8 updates and would exceed OneCycle's total.

The new guard rejects mixed sources unless the batch size divides the original read-block size. Under that restriction, all complete spans contribute an integral number of batches, and the one partial span contributes the same remainder regardless of its pool. The total is consequently `ceil(N/batch_size)` for every permutation. The guard runs before device transfer or optimizer setup. The new 19-row fixture checks rejection; the parent reports it GREEN. Supporting arbitrary nondividing configurations later would require counting the actual emitted batches or carrying tails across pools.

## Correct objective and denominator

For the actual training population of a particular fit, define:

```text
N  = number of eligible training arrivals
wᵢ = approved service-date weight for arrival i
μ  = (Σᵢ wᵢ) / N

Lweighted(θ) = (Σᵢ wᵢ ℓᵢ(θ)) / (N μ)
Lbatch(θ)    = (Σᵢ∈batch wᵢ ℓᵢ(θ)) / (batch_rows μ)
```

Compute `μ` once from exactly the rows being fitted, using a float64 sum of the generated weights and the full row count. Keep it fixed across batches, pools and epochs. A prefix resource probe has its own population and mean; it must not supply the mean for a subsequent full fit. Validation/test dates never enter this calculation.

The inspected `WeightedSource.__init__` streams the source's date column in original read spans and does this calculation. The trainer uses `numerator/(len(idx)*mean_weight)`; `normalized_weighted_loss` expresses the same formula. The remaining per-batch weight sum is used only for the epoch's diagnostic metric. Control weights and their mean are exactly one. Losses are averaged over ensemble members **per arrival** before the date weight is applied.

With the supplied audit's mean 1.0252102863, the effective old-row coefficient is approximately 0.97541 and the maximum recent-row coefficient is 1.46311. Their ratio remains 1.5. Requiring literal effective coefficients 1 and 1.5 after global normalization would be the wrong test. The reported recent rows comprise about 9.84% of arrivals and about 12.05% of weighted mass, assuming all nonrecent weights are one; this is a modest change in emphasis.

At a fixed parameter vector, any partition of the same rows must satisfy:

```text
Σbatch (batch_rows/N) ∇Lbatch = ∇Lweighted
```

This is a useful exact gradient contract, including an unequal final batch. It is not an assertion that sequential Adam updates equal one full-dataset update, or that bounded pool sampling is an independent global row shuffle.

## AdamW and what demonstrates a real treatment

For a common positive gradient scale across the entire history, Adam's first moment scales by `c` and its second moment by `c²`. In the ideal zero-epsilon calculation, their ratio is unchanged. Finite epsilon, changing scales, moment history and learning-rate schedules make the actual case less exact. AdamW's decoupled parameter decay is not multiplied by the loss weight.

Thus, preserving a 1.5-times **gradient** in a homogeneous batch is necessary to catch the old denominator defect, but a 1.5-times **parameter update** is not a valid expectation. Mixed batches are valuable because weights change the relative contributions of old and recent examples within the gradient vector.

A decisive small example uses one parameter, squared-error targets 0 for an old row and 2 for a recent row, and weights [1, 1.5]. At parameter 1, the unweighted gradient is zero and the globally normalized weighted gradient is -0.4; their optima are 1 and 1.2 respectively. The newly added binned-loss fixture now checks the analogous opposing-class case: equal-weight gradients cancel, while the recent-class weight changes the gradient direction before Adam. This demonstrates a changed objective rather than only a common scalar. A fit whose **entire population** has one constant weight should intentionally remain equivalent to control after normalization.

## Bounded sampler and paired experiment

The current mechanism is reasonable:

1. Split the fit into the original contiguous 262,144-row spans.
2. Independently permute the span order each epoch with the fit's seeded NumPy RNG.
3. Group up to eight spans, concatenate aligned features, targets and weights, and shuffle the pooled rows with `torch.randperm`.
4. Take ordinary mini-batches from that row permutation and visit every source row exactly once.

The maximum full pool is 2,097,152 rows. No full-fit row permutation is allocated. Weight calculation consumes no RNG, so control and weighted sources follow identical span, row-shuffle and dropout RNG calls given the same setup. Their paired training keys and report checks also bind configuration, source/input identities, warm start, selection rules and device.

Random eight-span grouping mixes dates **probabilistically**. It does not guarantee distant dates in every pool or every batch; all-old pools are expected when recent rows are only about a tenth of the population. This does not itself invalidate the objective. Audit the actual batch composition and the fraction of recent rows sharing batches with older rows. Concatenation alone would be insufficient without the subsequent row permutation, which the current trainer performs. Use bounded date strata only if observed exposure is inadequate, and apply the same revised mechanism to both conditions. Equal-date quotas, recent oversampling, or duplication would change the empirical objective and need their own correction/design.

Both controls must be refitted with the new sampler. Comparing a newly mixed weighted fit to the old date-local control would confound weighting with sampling. Retain the existing four-fit design, matched row/epoch budgets, architecture, initialization, learning-rate/decay settings, precision, seeds and checkpoint-selection policy. Existing classifier views share their underlying fit. Do not resume optimizer state from a diagnosed fit under the new objective. The common approved fine-tuning warm start remains a separate, paired initialization.

## Narrow experiment, reuse and report review

| Change | Inspection result and practical condition |
|---|---|
| `recency_experiment.directory` | Resolves absolute or repository-relative `DBREL_RECENCY_EXPERIMENT` paths and requires containment in both the repository and its resolved `data/ml` subtree. It rejects the subtree root and escaping paths/symlinks. Select the new tree before importing/running the process; the default still names the preserved first tree. |
| Trainer isolation | The version-4 key binds the normalization policy, mix/read/pool dimensions, dataset mean, script/weight-source identities and fit inputs. Changed keys refuse existing run directories. A fresh experiment tree preserves the diagnosed checkpoints/results. |
| Controller propagation | Controller and trainer use the shared selector. Every child receives an explicit `DBREL_RECENCY_EXPERIMENT` argv environment assignment. This avoids inheriting a different default in discrete processing or reporting. |
| `verified_prerequisites` | Requires the five correction/progress/baseline/August key/result identities; validates repository-local resolved paths and lowercase SHA-256 values, then stream-hashes every supplied file, including extra pins. Missing or changed evidence fails before any fit. Hash matching binds previously verified evidence; it does not independently prove that the measurements were scientifically correct. |
| `--reuse-prerequisites` | Removes the two measurement jobs from the new queue only after that verification and records reused completion provenance. Use this flag for the new attempt so the preserved original measurement tree is consumed without rerunning its producers. |
| Summary input roots | New candidate arrays and output summary use the selected experiment. Baseline/August consumers deliberately use `DEFAULT_RELATIVE`, preserving the original measurement reference. Chromium readiness is read via `experiment_directory(root)`, so it follows the candidate experiment. |
| `verify_sampler_pair` | For version-4 keys, validates positive integer dimensions, pool-size consistency, mixing of at least two spans, exact-coverage declaration, fixed normalization and valid means; requires identical sampler metadata except for the condition-specific mean. The caller additionally checks shared fit/configuration identities and amplitudes 0/0.5 with matching ramp dates. |
| Historical report compatibility | The both-pre-v4 bypass is for historical fixtures. Live candidate consumers still verify the training/weight source fingerprints; old completed keys cannot legitimately pass against the new runner. Do not treat historical fixture acceptance as certification of the old treatment. |
| Report provenance | Candidate records include sampler metadata and key/result/prediction identities. These describe the configured mechanism; batch-exposure diagnostics remain separate evidence. |

No additional defect was found in these narrow paths. This does not re-review the broader scoring, export, browser implementation or training architectures.

## Test evidence and recommended contracts

The parent reports **62 focused comparison tests passed**, 449 MiB peak under a 2 GiB cap and zero swap. This includes five further gradient cases added during the final document check and inspected here. This reviewer read the tests and source; **no tests, training, inference or scoring were executed in this review**.

| Contract | Evidence inspected / next useful check |
|---|---|
| Constant-date cancellation is removed | Helper gradient-scale test and actual-trainer pre-Adam gradient capture in `test_recency_mixing.py`; the latter protects the production loop even though it spells out the formula instead of calling the helper. |
| Existing arrival-loss semantics | `test_recency_weights.py` checks both heads and member counts. Four newly added fixed-theta gradient cases check unequal partitions for quantile/binned heads and 1/3 members; a fifth case checks opposing binned-cohort gradients. Source inspected; parent reports all cases passed. |
| Row/date/weight alignment and bounded reads | Mixed-pool fixture checks deterministic pools, control/weighted row equality, exact once-only coverage and original-span read limits. Include a supported partial original span and a short final pool in batch-count/coverage checks. |
| Actual date mixing | Inspect or record sampled **mini-batch** row IDs/date weights, not only unique dates per concatenated pool. Hash the paired batch schedule and compare control to weighted. All-old batches are allowed; recent exposure is the relevant diagnostic. |
| Resume and setup rejection | Mixed-source fixture compares continuous and epoch-boundary resumed model, optimizer, scheduler and RNG states. Existing setup rejection and versioned run keys protect policy/source changes. These checkpoints are at epoch boundaries; no mid-epoch resume guarantee is implied. |
| Scheduler | New partial-span/nondividing fixture rejects the unsupported configuration. For supported configurations, verify actual update count equals the declared scheduler count and that no tail rows are dropped. |
| Isolation/reuse/report gates | Selector fixtures cover relative/absolute paths and escaping symlinks; reuse fixtures cover missing/changed/escaping pins; sampler gate fixtures cover missing metadata, local normalization, nonfinite mean and inconsistent/different dimensions. Readiness root selection was also checked directly in source. |

The supplied new `data/ml/recency_training_mixed_20261014/date-exposure-audit.json` covers all 88,948,878 training rows and 8,749,367 recent rows. With batch size 8,192, it reports 10,859 batches, including 5,867 mixed old/recent batches for seed 0 and 6,635 for seed 1, with no all-recent batches. Its mean is 1.0252102862781092 and effective coefficients span 0.9754096436–1.4631144655. This supports the bounded mixing design. Its stated scope is production first-epoch **span grouping with a representative NumPy row permutation**; it does not reproduce Torch/dropout/GPU RNG or certify each actual training batch. This reviewer read the small audit JSON, not the underlying date column. A paired actual batch-schedule hash remains a useful diagnostic. Prefix probes containing only old dates verify resource behavior, not the recency treatment.

## Resource and empirical limits

The trainer still transfers an entire pool to the device, including int64 categories, labels/bins and a row permutation. During generator advancement, the caller can retain the previous host pool while the next pieces and concatenated pool are built. Memory is bounded by pool size, but peak memory can exceed one pool's payload. The existing tensor guard checks individual tensor limits, not combined process/GPU memory.

The parent reports two full pools per architecture passed under 8 GiB and zero swap, with cgroup peaks of 2.263 and 1.886 GiB and GPU allocated memory around 0.75 GiB. The training runner hash remained unchanged. These source-matched probes support feasibility of the pool transition; this reviewer did not execute them or independently inspect their raw logs. Preserve their resource records and retain the cap for all four serial fits. Tiny CPU fixtures alone would not have certified this peak. If a full run exceeds the cap, retain host pools and transfer indexed mini-batches, or reduce the common mixing configuration and rerun paired checks; do not silently alter only one condition.

Correct loss/gradient and sampler contracts establish a valid treatment implementation. Improvement requires the newly matched model comparison on the unchanged corrected cohort, with ordinary unweighted forecast metrics, verified/unresolved coverage, calibration and paired date-level uncertainty. Keep selection separate from later comparison. The already inspected later dates remain reused evaluation data, and this one fixed-seed comparison is not independent confirmation or a multi-seed estimate.

The audit was supplied, not independently recomputed from real rows. No real dataset, model, GPU, browser, production database, network or job was opened/executed for this review. The database was only stat-ed; unchanged metadata is not a new byte-identity certification. No source/test edits, commits or subagents were made. Only this review document was written.

## Inspected SHA-256 identities

| File | SHA-256 |
|---|---|
| `scripts/train_recency_delay.py` | `aaca3721f0fabc3bb25cfb5ad11ada40b173adc1a6453905d1bb8cd08cb3af12` |
| `scripts/recency_weights.py` | `ee70504cc4ad7bcfa8b6f97b5528eb0b925e45ec5e15ebc69cf78790854eff9e` |
| `scripts/recency_experiment.py` | `765908c47e5456c5cc5e74b205fdfe1a846d5e32c95090f1b0ee79767c356ef2` |
| `scripts/run_recency_training.py` | `30797baf03d877b603aa0052bd309689be52b3a43c9471cdab0b7558db2399e5` |
| `scripts/summarize_recency_training.py` | `844976844e984b9ede1f9727b222dfa555274c348e5b24023d54bc6daf29b3ac` |
| `scripts/recency_provenance.py` | `51dae331d2e6ad7171d914f617de6484f3508aa81884d9d9c7d5015d82261f3f` |
| `tests/test_recency_mixing.py` | `8461cc8bfc32c50d3bfebfc3d4066f76b908fb81e84550133f226758fc5dd66e` |
| `tests/test_recency_weights.py` | `cbff7bf017beb8bd53b481f9611871637db777c7db3a2c5f4d453ce5fe33b5ce` |
| `tests/test_recency_experiment.py` | `43a14c808b69849ab586181eb5cad7baae26bc8e448e5164f1d6ec3ce516626e` |
| `tests/test_recency_prerequisite_reuse.py` | `cd997a3da2a1fcf00aa1418f1832bd5101e93a4f0adf4d5bd62fa966d3e3d5be` |
| `tests/test_recency_sampler_gate.py` | `9c7cadde8a590a2aae850befd1bbea32067431dd0ce089d540ec0f25f59a9209` |
| `src/dbrel/ml/arrival_nn.py` | `899293291eec6b7d776bdef0deba0900a57e1b595cd433051b19e398f88ccef8` |
| `data/ml/recency_training_20261013/weight-layout-audit.json` | `3cb6fca2d3d7fea4527e2079605b7c2074683c314dc740e5cc745432cb24e7ac` |
| `data/ml/recency_training_mixed_20261014/date-exposure-audit.json` | `ad9eab56ab8c64a1a2b1f802c85dd62259b18103291c38af0e819b045dc186e9` |
