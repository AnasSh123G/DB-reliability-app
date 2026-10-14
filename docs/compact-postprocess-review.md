---
kind: review
title: "Discrete delay quantiles and validation reporting: independent review"
---

Reviewed on 2026-10-13 against [the training plan](compact-training-plan.md). Scope: `scripts/check_discrete_delay.py`, `scripts/summarize_compact_training.py`, and the added `scripts/plot_compact_training.py`, including their new `compact_provenance.py` dependency and existing arrival export/browser contracts. The reviewer edited only this document. The active, previously reviewed training runner was not changed.

## Result

**No remaining actionable correctness findings in the final reviewed source snapshot.** The implementation agent addressed the findings below during review. Synthetic CPU/ONNX/WASM checks passed; this does not certify the full trained candidate or replace its browser and population validation.

## Resolved finding: validation of day clusters

The first summary consistency correction checked only frozen **overall** metrics. A reordered point array could preserve every overall metric while changing the paired daily differences and their bootstrap intervals. This is particularly relevant with many identical integer outcomes.

Reproduced entirely in memory with three distinct days:

```text
actual:              [0, 0, 0]
reference:           [0, 6, 6]
frozen candidate:    [0, 0, 6]
reordered candidate: [6, 0, 0]

Both candidates: MAE = 2, within-one/three/five = 2/3, coverage = 1.
Within-five difference interval: [0, 1] becomes [-1, 1].
MAE difference interval:        [-6, 0] becomes [-6, 6].
```

Both arrays passed the earlier overall consistency loop. The final `verify_groups()` correction recomputes every raw metric per day and per segment, validates group keys, and checks the existing frozen results before bootstrapping. A CPU check confirmed that the original predictions pass and the reordered array is rejected at the per-day check. The existing frozen trainer did not need to change.

## Findings addressed during review

| Finding | Correction inspected |
| --- | --- |
| Exact CDF comparisons selected different integer classes across runtimes | NumPy and Torch use the same float64 thresholds `q - 1e-6`; the fixed probability tolerance is recorded in the transform metadata and disclosure |
| Accuracy was scored from GPU probabilities while deployment used a separate exported decision | Both validation fortnights are now scored directly through the exported ONNX CPU graph; the metric runtime is identified |
| Nesting the old training key did not validate current inputs or implementation | `verify_training_key()` checks live runner/production/reference/input hashes, fit/eval keys and metadata, prep/vocabulary signatures, and prepared-column size/mtime identities |
| Saved weights could be detached from the claimed selected epoch | The discrete runner requires a binned source and compares `best.pt` tensors with the selected epoch checkpoint before export |
| Summary comparisons relied on equal row counts | Reference stop IDs are checked in bounded blocks, candidate training identities and discrete source hashes are verified, and frozen overall metrics are checked |
| Overall agreement could conceal a changed daily bootstrap or segment result | Recomputed per-day and per-segment scores are checked against their frozen results; the reordered-prediction counterexample is rejected |
| Invalid edges could yield NaN or unordered quantiles | NumPy now requires finite, one-dimensional, strictly increasing edges and validates finite positive probability-row totals |
| Plotting could create Matplotlib configuration/font caches outside the repository | `MPLCONFIGDIR` is set to the experiment's local temporary directory before Matplotlib/pyplot imports |

## Numerical reproduction and verification

The original exact comparison had a reproducible failure on the pinned ONNX Runtime Web **1.30.0** WASM provider. The actual `DiscreteExport` class was extracted from source and wrapped around a stateless logits passthrough, without loading trained weights. With 150 classes labelled -30 through 119, generate 300 rows using:

```python
rng = np.random.default_rng(4231)
half = rng.normal(0, 2, size=(300, 75)).astype(np.float32)
logits = np.concatenate([half, half[:, ::-1]], axis=1)
```

The palindrome gives a mathematical midpoint CDF of exactly 0.5. Before the correction, Torch CPU and the installed WASM runtime disagreed on **140 median elements by one minute** (44 versus 45). A uniform 150-class distribution also chose 45 instead of the first mathematical median class, 44. Converting the cumulative sum to float64 alone did not prevent the rounding failure.

After the shared boundary policy was added, the same 300 rows plus a uniform row gave **exact NumPy/Torch/WASM agreement, zero differing elements, and all 301 medians equal to 44**. The uniform p25/p50/p75 were `[7, 44, 82]`. The exported output was float32 `[301,3]`, and the WASM provider supported the graph's float64 cumulative operations. The corrected CPU ONNX graph also agreed exactly at dynamic batch sizes **1, 7, and 301**. The graph and inputs travelled through memory and standard input; no model files or outside temporary files were created. Earlier CPU ONNX checks passed for uniform, concentrated, exact-quarter-mass, graded, and endpoint distributions.

Additional small CPU checks passed for the app display boundaries (`point < 1` maps to zero, otherwise `floor(point + 0.5)`), paired day resampling, row-count weighting, deterministic bootstrap output, rejection of malformed edges, and numeric/absent-metric equality. All four new scripts compiled without writing bytecode. The authoritative UI implementation is `bahn-buddy-predict/src/components/v2/delay-parts.ts`. Source inspection confirms resampling whole days preserves the pairing of reference and candidate. The intervals remain diagnostics over reused validation, exclude selection uncertainty, and do not certify future performance or inter-day independence.

## Compatibility, scope and resource limits

- The artifact keeps the **actual binned training config** and records the inference transform separately. Its public `output: quantiles`, `edges: null`, `point_rule: median`, and ordered float32 `[N,3]` match the unchanged Python/ONNX/browser quantile branch. The ±window outputs remain separate distribution diagnostics.
- The endpoint classes censor labels below -30 or at/above the upper training range; the discrete output is a quantile of those class labels. The manifest records the endpoint policy. The broader training-label integer audit is pending; this review does not certify that all 88,948,878 training targets are integral. Validation integrality is enforced by the scoring loop, and the supplied context reports the full 7,967,853-label check.
- Budget counting is sound: `save_arrival_artifact(role="deployable")` measures ONNX plus the complete serialized manifest and rejects anything above 35,000,000 bytes. The result now derives its strict-budget flag from that measurement. The earlier literal `True` was protected by the same strict writer gate; it was not an oversized-artifact acceptance bug. Training-only notice metadata is retained, verified through the existing read-only notice loader, and no promotion path is called.
- Export retry preserves prior local ONNX directories. A completed discrete run is not overwritten, and its source weights, transform, dependency/runtime versions and provenance helper are fingerprinted. Prepared binaries use the original runner's size/mtime identity contract, not a content hash of all 89 million rows.
- Scoring reads bounded batches. No training, GPU use, trained-model inference, dependency installation, subagent creation, database write, commit, or job polling was performed by the reviewer. The postprocessor's full candidate validation and real browser fixture checks remain the implementation agent's work. Synthetic numerical parity is not a certification of every future model input.
- The plot reads the already generated summary and exports local SVG/PNG files with a reused-validation subtitle. No other plotting correctness finding was identified. The reviewer did not run the plot or generate its font cache.

## Reviewed snapshot

These hashes identify the final source reviewed after corrections. The conclusions apply to this snapshot, not subsequent changes.

```text
fe2a53ad3537161542802535371a4a451919c0740cd111c9b84acf25246aa4d0  scripts/check_discrete_delay.py
fededa7b017e9c4c81ca567e217e6cb8728bad56dc546a32200177498d9b3407  scripts/summarize_compact_training.py
61d5bbeb91cfa505c762f1aefa06f018c85a55c2f5e8c90bf54d9c28b66d1c9c  scripts/plot_compact_training.py
aa289863d12e7b52399953034d85305a12f7fcc865a6a831e48ed9d2eb67ce1c  scripts/compact_provenance.py
b464e22d93e2fcb51c0e55a22962f5b85ef8c6b9e86121b27ff7dfad29e78d2a  scripts/train_compact_delay.py (unchanged)
```

## Browser launch and report-format follow-up — 2026-10-13

Scope: the short temporary-path and failure-reporting change in `scripts/explore_delay_browser.py`, plus the report-only table formatting change in `scripts/summarize_compact_training.py`. The numerical, training, discrete, plot and provenance code from the preceding review was not changed.

### P2 — Cleanup timeout prevents the browser failure report from being saved

**Status: resolved in the final cleanup snapshot recorded below.** The following describes the initial follow-up snapshot.

**Location:** `scripts/explore_delay_browser.py:163–169` in the snapshot below.

The new no-result branch sets an error in memory, but `proc.wait(timeout=10)` in `finally` can raise `TimeoutExpired`. This skips `server.shutdown()`, the `passed=False` calculation and `actual_frontend_browser.json` serialization. The enclosing temporary-directory context then removes the scratch directory while the process may still be running. A previous browser result, if present, is also left in place.

An in-memory test executed the actual launch/cleanup AST with a process stub whose `wait()` raises. It confirmed that the diagnostic was set, but `passed` was never computed and server shutdown was skipped. **Action:** after terminate, catch `TimeoutExpired`, kill and reap the child, guarantee server shutdown through a nested `finally`, and preserve the structured failure result. Launch-time `Popen` errors can use the same reporting path.

### Checks that passed

- The repository-root `.bt-*` scratch directory, child `TMPDIR` override, and `scratch/p` profile remove the long service temporary-directory prefix. A representative Chromium singleton socket path is 92 bytes at this checkout. The parent environment is preserved, and scratch/profile/stderr locations remain inside the repository.
- A simulated early Chromium exit produced a diagnostic containing the exit code, computed `passed=False`, and shut down the server. Each retry creates a fresh scratch/profile; stderr is retained in the candidate directory after scratch cleanup. This is a log of the latest attempt, since it is opened with `w`.
- The report formatter now appends all candidate table rows consecutively, then emits the artifact directory list after a blank line. An in-memory two-candidate fixture verified that layout. Numeric report inputs are unchanged, and both scripts compile without writing bytecode.
- The reviewer did not launch a browser, process, HTTP server or socket, load or score trained models, use a GPU, install dependencies, spawn agents, write source, or commit. Launch checks used stubs; only this review was written. The implementation agent reports all four actual Chromium checks passed; those real runs were not performed or independently inspected by this reviewer.

Follow-up snapshot:

```text
3109eae2acb6d1846a872d18474d88ca6236b2d5a52dbb6bb20e7e138b4b1f87  scripts/explore_delay_browser.py
20ebee85b86952e6d62d092bf7b7ecac866141c378e8d0db14c9b54ea115cdc4  scripts/summarize_compact_training.py
```

### Final cleanup resolution

**No remaining actionable findings in the browser launch/recovery or report-format changes.** The final browser snapshot puts `Popen` inside the protected block, records `OSError` as a structured launch failure, catches the terminate/wait timeout, kills and retries reaping the child, and records a second wait timeout as failure. A nested `finally` guarantees server shutdown. Normal numerical result handling is unchanged.

The reviewer re-read this current source and executed its actual launch, pass/fail calculation, runtime metadata and result-serialization statements against in-memory stubs. All five cases passed:

| Stub case | Result serialized | Server shut down |
| --- | --- | --- |
| Successful page result | `passed: true` | Yes |
| Chromium exits before posting | `passed: false`, exit diagnostic | Yes |
| `Popen` raises `OSError` | `passed: false`, launch diagnostic | Yes |
| First wait times out; kill/reap succeeds | `passed: false`, forced-shutdown diagnostic | Yes |
| Wait after kill also times out | `passed: false`, did-not-exit diagnostic | Yes |

Compilation passed. The tests created no browser, child process, HTTP server, socket, temporary directory or log/result file; serialization used an in-memory file stub. No trained model or GPU was used. The implementation agent's four actual numerical Chromium passes were obtained on the preceding short-path snapshot, before this cleanup-only correction; this reviewer did not rerun those real browser checks.

Final follow-up snapshot:

```text
fc061741b4ae36cc8d5bf2f0848de614f368b25a887a6d196be1310a2cafa673  scripts/explore_delay_browser.py
20ebee85b86952e6d62d092bf7b7ecac866141c378e8d0db14c9b54ea115cdc4  scripts/summarize_compact_training.py
```
