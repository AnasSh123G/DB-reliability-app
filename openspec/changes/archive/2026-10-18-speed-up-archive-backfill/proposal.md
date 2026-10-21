# Proposal

## Why

`dbrel backfill` is too slow to rebuild history. The last full-year build, on code before
`03d834a`, took 3–4 days.

On 2026-10-17 we measured the current code on real data. Replaying 2026-09-29 into a reflink
copy of the 51 GB national database (5,422 tracked stations) took **26 min 43 s for one day**.
It ran on one core at about 93% the whole time, with no I/O wait. At that rate:

| Window | Time |
|---|---:|
| The default 30-day window | about 13 h |
| A full year | about 6 days |

Profiling (py-spy, 1,600 s on the main thread) shows the time is not spent parsing (2%) or
writing to disk:

| Where | Share | Notes |
|---|---:|---|
| Finalize's trip selection | 23% | Walks all 13.5M TT trips through the wrong index. The database has no `sqlite_stat1`. |
| ORM bulk `UPDATE`s | 22% | Each one is evaluated against every object in the session's identity map. |
| `record_plans` | 16% | One query per stop, plus a bounds refresh and a reconcile on every call. |
| The rest of finalize | 9% | Includes loading all 7.3M `tt_poll` rows each day. |
| Per-call reloads | ~6% | The 250k-row train identity map and the trip index. |

Repeated work multiplies all of this: a run is upserted once per plan hour it appears in.

The 2026-10-14 explore session (`845fee87`) reached the same conclusions from a scratch-database
profile. It proposed four tiers of fixes. This change takes **tiers 0, 1 and 3** from that
plan, adds the costs only the real-database profile exposed, and drops tier 2, parse-ahead in a
worker process: parsing is 2% of the measured time.

## Correctness correction (2026-10-18)

Small-fixture review C1 disproved the fixed replay-date cutoff: older unfinished trips
can become eligible after missing or plan-only archive days. Replay keeps unbounded eligible
trip selection. Independence from unfinalized history remains an unmet goal, pending an
equivalent overdue-backlog mechanism; it cannot be claimed from the index optimization.

## What Changes

- **Phase A: run safely, time it, compare it.**
  - `backfill --done-file` keeps scratch runs from marking days done for the live database.
  - A writer lock keeps a backfill and `collect` from writing the same database at once.
  - `backfill --timings` prints plan, sweeps and finalize seconds per day.
  - An equivalence harness compares two databases row by row on natural keys. It is the gate
    for every later step: optimized code must produce the same database as the reference code
    on real archive days.
  - Investigate the `path does not match trip` abort that killed the 2026-10-14 scratch run,
    and fix it if it reproduces on real archive days.
- **Tier 0: database settings and indexes.**
  - Indexes on `trip(source, service_date)`, plus partial indexes for unfinalized trips and
    successful polls.
  - `ANALYZE`.
  - A larger `cache_size` and `mmap_size` while a backfill runs only.
  - Finalize loads only the polls its window needs, not all 7.3M.
  - Replay's finalize considers only the replayed day's service dates. This also stops it
    reloading the 255,548 never-finalizable trips every day.
- **Tier 1: same code path, less overhead.**
  - Trip updates go through the identity map instead of ORM bulk `UPDATE`s. This is the 22%
    item. The poller and the per-run writer still need it after tier 3.
  - Day-scoped context: train keys, the line cache and tracked stations are loaded once per
    day, not once per call.
  - Deferred settling of trip bounds and reconciliation.
  - One `station_stop` query per call instead of one per stop.
  - Cached executemany statements.
  - Each `/fchg` body is hashed once per station.
  - Optionally, behind a flag and gated by the harness: one `import_plan` per partition
    instead of one per hour.
- **Tier 3: batched plan writer.** `upsert_runs_batch` resolves every run of an
  `import_plan` call in one pass, with preloaded maps and bulk writes. It replaces the
  per-run loop for live `/plan` import and replay alike, and is accepted only if the harness
  shows it equivalent.
- **Measurement and reporting.**
  - Per-tier benchmarks on fresh scratch databases and on reflink copies of the live
    database.
  - A report comparing rebuilt days with the current live database.
  - A projected full-year time.

Nothing here is **BREAKING**.
- Schema changes are additive: indexes only.
- Behind the opt-in per-partition plan import only, `train.n_runs_seen` and
  `train.n_stops_tracked` may change. Both are display-only counters, and the deployed build
  already drops them.

Out of scope:
- **Rebuilding the live database.** That is a separate decision for the user after this
  change reports.
- **Tier 2 (parse-ahead).**
- **The route-fingerprint drift found on 2026-10-17.** 225,200 trips have no stops because
  the run's digest depends on station-name spelling. Fixing it changes which rows are
  written, so it cannot pass this change's equivalence gate. It is a follow-up change.
- **Any automation, ML, web or deployed-database code.**

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `archive-backfill`: adds requirements for:
  - a stale run reference no longer aborting a day;
  - optimized replay producing the same database as the reference implementation;
  - replay throughput being measured, with a ≥3× speedup on a copy of the live database;
  - replay cost not growing with accumulated history;
  - bulk-load tuning being confined to the backfill;
  - backfill and live collection never writing the same database at once;
  - a report comparing a rebuild with the current database.

## Impact

- **Code:**
  - `src/dbrel/ingest/replay.py`, `plan_import.py`, `runs.py` (and a new
    `runs_batch.py`), `dated_schedule.py`, `schedule_reconcile.py`, `changes_poller.py` and
    `finalize_tt.py`;
  - `src/dbrel/db.py` (backfill-only pragmas), `src/dbrel/models.py` (indexes) and
    `src/dbrel/config.py`;
  - `src/dbrel/cli.py`:
    - `backfill` options `--done-file`, `--timings`, `--bulk-pragmas/--no-bulk-pragmas`,
      `--merge-plan-hours/--no-merge-plan-hours` and `--plan-writer`;
    - the new command `migrate-backfill-indexes`.
- **Live collection** shares `import_plan`, `run_tt_poll` and `run_finalize_tt`:
  - it gets the identity-map fix and the batched writer, the latter only after the gate
    passes;
  - its connection settings do not change;
  - its output must not change.
- **New code under `scripts/` and `src/`:**
  - `src/dbrel/ingest_compare.py`, the library behind `scripts/compare_ingest_dbs.py`;
  - `scripts/seed_scratch_db.py`;
  - `scripts/bench_backfill.py` and `scripts/summarize_bench.py`;
  - `scripts/compare_with_live.py`.
- **Tests:** new unit and differential tests. The existing suite must stay green.
- **Data:**
  - Archive partitions for the test windows are downloaded again, about 4 GB, into
    `data/scratch/backfill-speed/archive`. The local archive was cleared and holds only its
    manifest.
  - Scratch databases and reflink copies go on the btrfs volume, which has 221 GB free.
  - The live `data/dbrel.sqlite` is only ever read or reflink-copied, never written.
- **Docs:** the `README.md` backfill section, and a new `docs/backfill-speed-results.md`.
- **Supersedes** the 2026-10-14 draft of this change. That draft was untracked in the main
  checkout and was deleted on 2026-10-17; this change is the only copy.

## Execution revision (2026-10-18)

At the user's direction, implement the fixes against a small one-day/24-station sample
and fast regression tests first. Defer broad historical and full-database replays until
the optimized code passes that sample. Preserve the frozen Phase A reference and all
correctness/throughput acceptance criteria. Use high or lower reasoning for subagents,
never max. The original long reference coordinator was intentionally stopped.
