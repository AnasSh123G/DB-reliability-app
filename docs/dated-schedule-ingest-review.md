---
kind: review
title: "Review: dated schedule ingestion and Chromium verification pin"
---

**Latest follow-up status:** both previously outstanding P2/P3 findings are resolved in the two reviewed deltas. The original review below is preserved as written; the resolution, newer source hashes and limits are appended at the end.

Reviewed October 14, 2026, against the working tree based on `2d6b28ff51885c9d16bf51026a41f08f1cdb260c`. Source hashes below identify the final inspected snapshot; later edits need their own verification.

**Outcome:** the cross-date regression and the three initially blocking defects have been addressed. One validated P2 remains for actual-time interpretation during the autumn clock change. The narrow Chromium pin/controller/report change addresses the requested pinning gap; this review does not certify execution of the real browser comparisons or the production migration.

## Remaining findings

### P2 — Corrected planned DST folds are still compared with uncorrected actual folds

**Where:** `src/dbrel/ingest/changes_poller.py:384` and `:566`; context in the unchanged `parser.py:29` and `runs.py:106`.

The parser selects `fold=0` for ambiguous Berlin wall times. `fix_dst` and the new receipt normalization correct **planned** times along the run, but `run_tt_poll` passes the independently parsed `changed_ts` directly into `_fact_row`. It can therefore publish a confirmed delay against inconsistent UTC interpretations.

A source-only, two-station SQLite fixture reproduced this on October 25, 2026:

| Event | IRIS wall time | Chronologically consistent UTC | Stored UTC |
|---|---|---|---|
| Origin planned/actual departure, sequence 1 | 02:50, first occurrence | 00:50 | 00:50 |
| Destination planned arrival, sequence 2 | 02:10, second occurrence | 01:10 | 01:10 |
| Destination actual arrival, sequence 2 | 02:20, second occurrence | 01:20 | **00:20** |
| Both change-response receipts | 03:10 | 02:10 | 02:10 |

The dated plan is unique and correctly normalized. Polling nevertheless stores `arr_delay_s=-3000` and a destination arrival **before the recorded origin departure**. The only UTC interpretation consistent with the observed journey yields `+600` seconds.

This is an existing actual-time limitation exposed by the corrected dated plans, not a failure of the now-fixed overlapping-plan normalization. The current DST regression checks planned receipts only. Add a recorded-arrival regression spanning the repeated hour. Resolve actual folds using available run/event chronology and the response receipt; when the evidence cannot distinguish them, suppress the confirmed time/delay rather than choosing the closest scheduled time. Keep the frozen parser and running correction untouched; implement containment in the future ingest path.

### P3 — Append deduplication still rereads a trip's entire receipt history

**Where:** `src/dbrel/ingest/dated_schedule.py:117`.

The resolver now selects distinct planned values instead of hydrating every receipt object, fixing the large repeated-receipt allocation found in the initial review. However, each `record_plans` call still selects and materializes all existing receipt identities for that dated trip. Known full-change records call this once per stop, so append work grows with repeated receipts and repeatedly revisits earlier evidence. Scope duplicate checks to incoming identities or use the existing unique constraint for conflict-safe inserts. This is a follow-up efficiency item; no production-scale timing or memory claim was made.

## Resolved findings and checked contracts

| Initial defect or requirement | Final implementation / evidence |
|---|---|
| **P1:** a known full `/fchg` record overwrote the whole trip using one stop, shrinking its endpoints/count and sometimes changing its path | Known trips append via `record_plans`; only unknown runs call `upsert_run`. Independent recheck retained the two-stop route and its 13:00 endpoint and produced no future destination gap at 12:20. |
| **P1:** a late conflicting plan left an earlier `RECORDED` delay and trip rollup confirmed | `schedule_reconcile.reconcile_facts` refreshes derived references and delays, preserving actuals, quality and CT receipt timestamps. Independent recheck cleared both delays and trip final/max delay while preserving the 12:10 actual and 12:20 receipt. |
| **P1:** archive replay discarded older plans from the same bucket | `Response.plan_history` and `import_plan` retain every valid original receipt. Independent recheck stored both conflicting plans and resolved the affected arrival to NULL. |
| Future plan receipts were used during earlier change replay | Polling now resolves with `as_of=received_at`; finalization uses `as_of=now` and reconciles existing facts at its cutoff. |
| Overlapping DST plan copies manufactured a one-hour conflict | `upsert_run` clones stops/events and normalizes each receipt in run context. The added planned-DST regression passes; actual DST interpretation remains the P2 above. |
| Partial plan refresh shrank a known run's tracked extent | `refresh_run_bounds` aggregates the dated evidence for the trip's own path. Endpoint values are conservative closure bounds; a certified stop delay still requires a unique per-side reference. |
| A platform-only change was skipped by value comparison | `planned_platform` is included in `VALUE_FIELDS`; reconciliation also refreshes it. The independent late-conflict recheck cleared the stale platform without needing a changed CT. |
| A malformed latest archive plan discarded valid earlier history | The latest parse error remains visible, valid history is processed, and errored HTTP-200 buckets are eligible for retry. Covered by `test_dated_schedule_recovery.py`. |
| Existing incompatible snapshot tables were treated as migration success | Ensure/require guards now validate required column affinities, primary key and the receipt identity constraint. The new incompatible-table test initially failed during implementation and is included in the parent's passing final suite. |
| Pure cancellation deltas without PT or CT were classified as pending/NO_DATA | Explicit cancellation survives the earlier per-side filter. Regressions cover cancellation timestamps present and absent and require NULL actuals/delays. |
| Recurring train/date separation, station/index/path variants, repeated station visits, midnight and GTFS own-feed compatibility | Inspected central resolver and callers; focused integration/contract tests cover these shapes. TT missing dated evidence has no static-time fallback; per-side plan conflicts remain unknown. |
| Untrusted receipt values | Validation rejects invalid receipt kinds/hashes, booleans in integer fields, invalid calendar dates/hours, negative/fractional planned timestamps, missing station mappings and station/receipt disagreement. |

The `as_of` distinction matters: classifying CT as an actual requires its own response receipt. A late official plan can still be retrospective label evidence, and later reconciliation may repair or invalidate derived delays without rewriting the actual observation. That does not make the late plan a forecast-time feature. This review does not extend the approved correction into historical input-feature replacement.

Migration is additive and does not populate old dated evidence. No historical re-ingestion or source database activation was performed. The production migration and any collector startup that creates schema must wait until the frozen-data comparison releases the database invariant.

## Validation and limits

| Evidence | Result / attribution |
|---|---|
| Reviewer initial focused ingest suite | 38 passed; 76.3 MiB peak, zero swap. |
| Reviewer integration recheck after the first fixes | 61 passed; 86 MiB peak, zero swap. |
| Reviewer run while the incompatible-table regression was being implemented | 68 passed, one expected schema-guard RED failure; 90 MiB peak, zero swap. That failure is addressed in the final inspected source. |
| Parent final ingestion verification | Parent reports **227 passing tests**, **86.50% combined statement/branch coverage**, 2 GiB cap and zero swap. Not independently rerun, as requested. |
| Parent final recency CPU verification | Parent reports **40 passing CPU tests**. No training or real-browser run performed by this reviewer. |
| Independent synthetic probes | Reproduced the initial defects, rechecked their fixes, and reproduced the remaining actual-DST P2. All ran in memory-capped, zero-swap systemd units using in-memory SQLite; no models or real datasets were loaded. |

Only this review document was edited. No source edits, DB connections to production, commits, network calls, GPU work, model jobs or subagents were used. Temporary pytest files stayed under `.test-tmp/dated-schedule-review`.

`data/dbrel.sqlite` was never opened by this review. Its observed metadata stayed `size=51530735616`, `mtime=1791394868`, `inode=25588406`. This is an access/metadata check, **not** an independently recomputed 51.5 GB content checksum or certification of migration on that database.

The frozen source hashes remained:

```text
5a5927deeaa0891fc4a4c6ad9812f6bf10f1590cd1411e8eda7aced3a2a9b3ca  src/dbrel/ingest/parser.py
fb7c0635c76f3bbb033785b83b15ba8f6db0c1d0ce4b1f328dcc9489f3439e2c  src/dbrel/ingest/archive.py
01eb40c073d17fc42b0d4627c4930733949b4bacfaea6342b3fcc93c6137de6f  scripts/build_trip_schedule_corrections.py
```

## Appendix: Chromium pin, controller and report wiring

Scope was limited to `scripts/check_recency_browser.py` and the browser identity/runtime wiring in `run_recency_training.py` and `summarize_recency_training.py`. The earlier checker remains separate and was not edited by this review. No broader training re-review was undertaken.

No new blocking finding was validated in this narrow change:

- The controller requires exactly `{path, sha256, version}` and checks the actual executable identity against readiness before scheduling jobs. Each browser stage receives the pinned path and hash explicitly; there is no executable auto-discovery fallback.
- The new checker streams the executable's SHA-256, requires nonempty version output, checks identity before and after execution, and fails on launch/timeout/shutdown or identity errors.
- Both controller and checker require installed ONNX Runtime Web `1.30.0`. The report rejects missing/stale Chromium identities, identities differing from readiness, incorrect runtime versions and incomplete/failed parity cases.
- Browser checker/controller/report sources, frontend predictor and runtime/compiler package metadata are included in the controller source fingerprint. It rechecks that fingerprint before each stage and stops subsequent stages after failure.

The approved readiness identity must select the intended Chromium executable. Inspection and stub CPU tests do not establish that six real candidate artifacts passed browser inference. Those browser stages and their resulting reports remain mandatory execution gates. This appendix addresses the pinning change only; it does not authorize source-database migration, publish a model, attest completed fitting, or verify installed runtime file contents independently of the package metadata and actual parity run.

## Final inspected source hashes

```text
283c0bfbcbbf5a72115d53cabe4cdef8d80262c6cd415f785e7a31f3e83f713a  src/dbrel/models.py
a78608692d4f4f8245b66134e692948564804ce824d43ad6960bd59950d82986  src/dbrel/cli.py
d75704e812d0bdc358e3dbc5ea43a0fd03135919d87305a7d1aac7becda88a77  src/dbrel/ingest/client.py
ac688102623b1351e335ac53d7d51cd4ba6bd83321df5b35aa040c856fd0da7e  src/dbrel/ingest/runs.py
d5877b6895e005ab93400eab10c3c411338a73088f60747e33f5c844e91d90d4  src/dbrel/ingest/plan_import.py
470264b057f90871292766358f561589f1b95b757bc58e093470e3eee98d2848  src/dbrel/ingest/replay.py
f35a64d61bd1bd7cc7187726cbb16d07f4d4bcedbbdd1e97556c32b0e6767d51  src/dbrel/ingest/dated_schedule.py
fb9beaa30a111455dccffab642c15681296f018211d04b7224ed39affaf4ce7e  src/dbrel/ingest/changes_poller.py
59fe92fbd845d017929614da9d4f7937c52c22f36e1a36a7a0eba0c5a64dca50  src/dbrel/ingest/finalize_tt.py
5824526a8af31956a420fe39982761023d2cd54cbf7e6e938846abc5eb555f1c  src/dbrel/ingest/schedule_reconcile.py
b581eb16c9a44ecae96337fb34f261f595eddf622c91387b987a3ec449e853d2  src/dbrel/ingest/schedule_schema.py
9eee139c20ba309ca74668cb6fc78b31cb2583cc1ec31e96f3a24e531536c7a9  scripts/check_recency_browser.py
1950f147445b5948e3754f824e00af70779daef33859af365a8db61ccd581513  scripts/run_recency_training.py
a68cf4faf64087249fb360bcef3d2dde3e42b462e1d837cda5737cf927cd45e2  scripts/summarize_recency_training.py
fc061741b4ae36cc8d5bf2f0848de614f368b25a887a6d196be1310a2cafa673  scripts/explore_delay_browser.py
```

## Follow-up review: actual DST and indexed receipt insertion

This cold check covered only the new `actual_time.py`, its `_has_happened` caller, the `record_plans` insertion delta and their relevant ingest contracts. The broader comparison and browser appendix were not re-reviewed.

**Resolution:** the original actual-DST P2 is contained, and the receipt-history P3 is resolved. No new blocking finding was validated in these two deltas. `actual_time.ambiguous_actual` detects both valid UTC folds before passage is certified; clear other sides and explicit cancellations retain their established behavior. Indexed `INSERT ON CONFLICT DO NOTHING RETURNING stop_sequence` replaces the raw receipt-history SELECT, returns the actual insertion count and reconciles inserted stop sequences. Run-bound aggregation and reference reconciliation still query dated evidence when needed; constant total append cost has not been claimed.

| Check | Independent result |
|---|---|
| Focused public ingest contracts | **43 passed**, 67.4 MiB peak, zero swap, under a 2 GiB hard cap. Includes four actual-DST tests, store/receipt contracts, dated ingest, planned-DST/platform tests and recovery/cancellation tests. |
| Actual-time helper | Both autumn folds are recognized. Clear neighboring times, an ordinary day's 02:20 and clear spring timestamps are unaffected. Cache bound is 2048 entries. |
| DST observation policy | Mixed ambiguous/clear sides keep the clear actual and delay; an ambiguous arrival remains NULL. A first-fold receipt cannot certify an event that could belong to the future second fold. Cancellation remains recorded without fabricated actuals. |
| Indexed insertion probe | On SQLite **3.51.0**, enforced a **999-parameter** limit. **121** mixed observations across four chunks inserted exactly **85** new receipts; including the initial receipt, total rows were **86**. Exact replay inserted **0**. Largest insert had **640** parameters, and no raw receipt-identity SELECT occurred. |
| Probe resource use | 41 MiB peak, zero swap, under the same 2 GiB cap. In-memory SQLite and source-only synthetic observations. |
| Parent final full ingest/archive/CLI verification | Parent reports **232 passed**, **87% combined coverage**, **114.6 MiB peak**, 2 GiB hard cap and zero swap. Includes the new multi-chunk/duplicate append tests. The full suite was not independently rerun in this delta review. |

The intended DST containment is deliberately incomplete reconstruction: ambiguous actual sides are excluded from certified actuals/delays even after a late receipt. This sacrifices some repeated-hour coverage and does not use a nearest-plan heuristic. Resolving such actuals later would require independently justified chronology and separate regression evidence. These changes do not repair old stored rows or activate ingestion against the frozen production database.

The insert path was exercised on the installed SQLite runtime with working `RETURNING`; another collector runtime and production-scale performance were not tested. Schema activation, historical re-ingestion, real datasets/models, GPU/browser execution and the broader model comparison remain outside this follow-up.

The production database was again never opened. Observed metadata remained `size=51530735616`, `mtime=1791394868`, `inode=25588406`; the parser/archive/correction hashes above remained unchanged. Only this document was edited.

The following hashes supersede the initial snapshot for the two deltas and identify the new regression file:

```text
5064494aaa0b0d811803d92d5451a52df48c562ff47b92db088c78a5b19a4ac4  src/dbrel/ingest/actual_time.py
2709841065bf0bef0bb6517700dfd5195a4910bd9b5ebd9587248f926516386d  src/dbrel/ingest/changes_poller.py
71b8c44d6023c9b7f92bb330d8f2657e3bdc21a7a36ea3b69fc93b79384237a9  src/dbrel/ingest/dated_schedule.py
e9707c9f2a8c0f263a9ee44c66da7035bc2d4dcfbbcab27b7f648f7fb7e17791  tests/test_dated_schedule_actual_dst.py
```
