# Tasks

Read `design.md` fully before starting. The 2026-10-18 revised execution order takes precedence over older per-step broad-window instructions. Section names (D1–D10, "Safety rules") refer to it.

Rules for every task:

- `W=data/scratch/backfill-speed`. Never write `data/dbrel.sqlite`.
- Every scratch command sets `DBREL_DB` and passes `--root $W/archive --done-file
  $W/<db>.replayed.json`.
- Multi-day runs go in a capped `systemd-run --user` unit ("Safety rules" item 4).
- No `git commit`, no `git push`.
- After each code task, run the tests it names with `uv run pytest -q <files>`.
- At the end of each group, run `uv run pytest -q -x tests/test_archive_replay.py
  tests/test_backfill_resume.py tests/test_dated_schedule_*.py tests/test_collect.py
  tests/test_plan_import.py tests/test_upsert_run_collision.py` plus that group's new tests.
  All must pass before the next group starts.
- When a gate fails, do not continue to the next group:
  1. revert that step's code, but keep its notes;
  2. write the cause in `$W/<group>-notes.md`;
  3. report to the user if the design does not say what to do.

## 1. Workspace, guardrails and archive

- [x] 1.1 Create `$W/`, `$W/logs`, `$W/bench`, `$W/reports` and `$W/archive`. Verify that
  `git check-ignore data/scratch/x` reports it as ignored, and that `git status` shows no
  scratch files.
- [x] 1.2 Record the starting state in `$W/baseline-state.md`:
  - `git rev-parse HEAD`, `df -h data` and `free -g`;
  - `ls -la data/dbrel.sqlite*`;
  - the `sha256sum` of the first MiB of `data/dbrel.sqlite`, plus its mtime;
  - the 2026-10-17 baseline (26 min 43 s for 2026-09-29) and the profile table from
    `design.md`.

  Verify the file exists. Task 10.4 re-checks the live file.
- [x] 1.3 Fetch the archive for every window (D2) into `$W/archive`, after checking
  `df -h data` shows at least 30 GB free. Run `uv run dbrel archive-fetch --root $W/archive
  --from <first> --to <last>` once per window: W1, W2, W3, WL, and WN+WH together
  (2026-09-29 .. 2026-10-05). Verify that each command reports "every hour in the window is
  covered", except the user-approved gaps: W1 2026-03-29 and 2026-03-30 at 00; W3 2026-08-27 05–06 and 2026-08-28 05–07. Record the downloaded GB in
  `$W/baseline-state.md`.

## 2. Phase A — run safely, time it, compare it, fix the path abort (D3, D4)

- [x] 2.1 Add `backfill --done-file PATH`, defaulting to `root / "replayed.json"`, used for
  both reading and writing the resume set. Verify with a new test in
  `tests/test_backfill_resume.py` that `--done-file tmp/x.json` writes `x.json` and leaves
  `root/replayed.json` absent.
- [x] 2.2 Add the writer lock to `backfill` (D4, "Also in Phase A"):
  - always `<resolved DB_PATH>.writer.lock`, shared by `collect` for custom databases too;
  - plus `TT_SWEEP_LOCK` when `DB_PATH` is the default database;
  - `collect.exclusive_lock`, with `AlreadyRunning` reported like `collect` does.

  Verify with a test: holding the writer lock in-process makes a second `backfill` exit
  non-zero with the AlreadyRunning message and write nothing.
- [x] 2.3 Add `ReplayResult.plan_s`, `sweeps_s` and `finalize_s` (`time.perf_counter`
  around the three phases), and `upsert_calls` through `RunCounters`. Add `backfill
  --timings`, which prints `timings <date> plan_s=… sweeps_s=… finalize_s=… total_s=…
  upsert_calls=… refs_refreshed=…`. Verify with a test on the synthetic replay fixture that
  the fields are non-negative and `total ≥ plan + sweeps + finalize - 0.01`.
- [x] 2.4 Write `src/dbrel/ingest_compare.py` and `scripts/compare_ingest_dbs.py` exactly as
  in D3, including the "`finalized_at` differs in any date" check under `--dates`. Verify
  with `tests/test_ingest_compare.py`:
  - identical databases give exit 0;
  - a changed `trip_stop.dep_delay_s` gives exit 1, naming the table, key and column;
  - a deleted snapshot gives `only_a == 1`;
  - the same content with a different surrogate id order gives exit 0;
  - `--allow train.n_runs_seen` hides only that column;
  - `--dates` restricts as specified;
  - a trip outside `--dates` whose `finalized_at` differs is reported.
- [x] 2.5 Write `scripts/seed_scratch_db.py` (D2). It resets `last_plan_hour`,
  `last_fchg_at`, `n_fchg_polls` and `n_fchg_errors`, and lists them in its docstring.
  Verify by seeding `$W/seed-W2.sqlite`: its `station` and tracked counts equal the live
  counts (`sqlite3 -readonly`), and `trip` is empty.
- [x] 2.6 Reproduce the path error (D4) with the current code plus 2.1–2.5 only:
  1. `cp --reflink=always $W/seed-W2.sqlite $W/repro-W2.sqlite`;
  2. replay W2 in a unit capped at 8G with `--timings`;
  3. record `MemoryPeak` and the timings in `$W/phaseA-notes.md`;
  4. if W2 passes, repeat with the W1 and W3 seeds.

  Also write into the notes that the 2026-10-17 real-database replay of 2026-09-29 did not
  raise it. Verify that the notes name the outcome: reproduced (which candidate) or not
  reproduced.
- [x] 2.7 If candidate 2 was reproduced, apply D6.0 now (task 5.1) as part of Phase A and
  re-run the failing window. Report to the user only if it still fails. If nothing was
  reproduced, skip 2.8 and 2.9 and write "not reproduced". Verify that the notes record which
  branch was taken.
- [x] 2.8 Implement the candidate-1 fix in `changes_poller.run_tt_poll` (D4):
  - `run_keys_by_trip`;
  - `_refresh_refs` after every `upsert_run`;
  - a path re-check before `record_plans`;
  - the current path used for `dates_by_train`;
  - `PollResult.refs_refreshed`, wired into `--timings`.

  Verify that the test in 2.9 passes.
- [x] 2.9 Add `tests/test_backfill_speed_pathfix.py` from the D4 sequence, or from the real
  traceback if that sequence does not reach the `existing_id` branch. Assert that there is no
  exception, the snapshot is under the current path, and the fact is written. Verify the test
  fails without the 2.8 change (stash only that hunk) and passes with it.
- [x] 2.10 Re-run the failing window on a fresh reflink copy of its seed. Verify that every
  day completes, that `refs_refreshed` > 0 is printed, and that the notes record the timings.
- [x] 2.11 Document `--done-file`, `--timings` and the writer lock in the README's
  "Backfilling history" section and in the Commands table. Verify that the section mentions
  all three.
- [x] 2.12 Create the reference implementation (D1):
  1. `git diff HEAD -- src tests scripts > $W/phaseA.patch`;
  2. `git worktree add --detach $W/ref HEAD`;
  3. `git -C $W/ref apply $W/phaseA.patch`;
  4. `uv sync --frozen --extra backfill` in `$W/ref`.

  If 2.7 pulled D6.0 into Phase A, it is part of the patch. Verify that
  `uv run --directory $W/ref python -c "import dbrel; print(dbrel.__file__)"` points under
  `$W/ref`, and that the Phase A tests pass there.

## 3. Reference baselines

- [x] 3.1 Create `$W/seed-W1.sqlite` and `$W/seed-W3.sqlite`; `seed-W2` exists already.
  Never write to a seed again. Verify that each prints its counts and has an empty `trip`.
- [x] 3.2 Write `scripts/bench_backfill.py` (D9), including `--root`, `--profile` (py-spy as
  parent, safety rule 5) and `--memory-peak`. Verify with a one-day run on a seed copy that
  `$W/bench/<label>.json` holds the day's timings, and with `--profile` that a py-spy file is
  written.
- [x] 3.3 Prepare immutable `seed-S.sqlite`: reference tables from seed-W2, with 24
  deterministic tracked stations spanning EVAs, including Bochum and Dormagen. Replay
  June 9 once with the frozen reference and save timings.
- [x] 3.4 Repeat S from an identical seed copy and compare both reference databases
  strictly. Require exit 0 before accepting S as the iteration oracle.
- [x] 3.5 Record S's scope and timings. Use S and focused regression tests for the
  implementation gates below; defer W1/W2/W3 broad runs to final validation. Apply
  D6.0 alongside Tier 0 before the remaining Tier 1 work. Never use max reasoning
  effort for any subagent.

## 4. Tier 0 — indexes, ANALYZE, bounded finalize, bulk pragmas (D5)

- [x] 4.1 Declare the four indexes in `models.py`, using `sqlite_where` for the partial
  ones. Verify with a test that `create_all` creates them, and that the partial-index SQL
  contains `WHERE finalized_at IS NULL` and `WHERE error IS NULL AND http_status = 200`.
- [x] 4.2 Add `dbrel migrate-backfill-indexes`: `CREATE INDEX IF NOT EXISTS` ×4 then
  `ANALYZE`, printing timings, with a docstring warning that the live database is the user's
  call. Verify that running it twice on a seed copy succeeds and the second run creates
  nothing.
- [x] 4.3 Make `_load_trip_index` use `ix_trip_src_date`, and the finalize trip query use
  `ix_trip_unfinalized`. Verify with `tests/test_backfill_speed_core.py`, which runs
  `ANALYZE` on a small database and asserts that `EXPLAIN QUERY PLAN` names each index. Use
  `INDEXED BY` or `+trip.source` only if needed.
- [x] 4.4 Preserve overdue finalization across arbitrary archive gaps (C1). Remove the
  replay D-1/D cutoff; retain the eligible-trip time bound and partial index. Verify tiny
  missing-day, plan-only and multi-day gap fixtures against the reference. Date-scoped
  optimization is deferred until an equivalent backlog mechanism exists.
- [x] 4.5 Bound `_poll_times_by_station` with `since` and add `latest_by_station`. Verify
  with a `run_finalize_tt` unit test whose fixture has polls before `since`, inside the
  window and after the last trip: the gap rows' `quality` and `poll_gap_s` must equal those
  of the old function, kept in the test as an oracle.
- [x] 4.6 Add bulk pragmas: `enable_bulk_load()`, `config.BACKFILL_CACHE_MIB` and
  `BACKFILL_MMAP_MIB`, the env overrides, and `backfill --bulk-pragmas/--no-bulk-pragmas`.
  Verify with a test that a normal connection reports `cache_size = -131072` and
  `mmap_size = 268435456`, and that after `enable_bulk_load()` a new connection reports the
  bulk values. A fixture resets the flag.
- [x] 4.7 Gate: copy `seed-S` to `$W/t0-S.sqlite`, run `migrate-backfill-indexes`, then run
  the `t0-S` bench on seed-S (one day / 24 tracked stations). Verify that `compare_ingest_dbs.py $W/ref-S.sqlite $W/t0-S.sqlite`
  exits 0, and record the speedup in `$W/progress.md`.
- [x] 4.8 Document `migrate-backfill-indexes` and the bulk-pragma options in the README. Say
  that running the command on the live database is a separate, user-approved step, taken
  after a reflink backup. Verify that the section exists.

## 5. Tier 1 — same path, less overhead (D6)

- [x] 5.1 D6.0: replace the ORM `update(Trip)` in `upsert_run`'s `existing_id` branch with
  `session.get` plus assignment. Make the poller's status update a Core statement with
  targeted `expire`. Skip this task if 2.7 already did it. Verify with tests:
  - the in-session `Trip` has the new `path_sha1` after `upsert_run`;
  - two polls in one session read back the correct `n_polls_seen` and `status`;
  - monkeypatching `sqlalchemy.orm.bulk_persistence` `_get_matched_objects_on_criteria` to
    raise does not break a replay of the synthetic fixture.
- [x] 5.2 D6.1: add `ReplayDayContext` and `day_ctx=` on `import_plan` and `run_tt_poll`,
  built once per day. In the poller, keep `station_id_by_eva` per sweep. Verify with a test
  that `train_by_key` is the same object across one day's calls, and that `day_ctx=None`
  still queries.
- [x] 5.3 D6.3: add `PendingSettle`, `pending=` on `record_plans` and `upsert_run`, a settle
  before a re-point in the `existing_id` branch, and creation in `import_plan` only when
  `day_ctx` is given. Verify with a test that replays a fixture with a mid-batch re-point
  twice, with and without `pending`, and compares the two databases with `ingest_compare`
  (exit 0).
- [x] 5.4 D6.4: in `record_plans`, use one `station_stop` query per call, dict rows, and the
  module-level `_SNAPSHOT_INSERT` executemany with RETURNING (with the fallback if needed,
  noted). Reuse the caller's `Trip`. Verify that all `tests/test_dated_schedule_*.py` pass
  unchanged.
- [x] 5.5 D6.5: use a module-level `station_stop` insert in `upsert_run`, and hash each
  `/fchg` body once per station in `run_tt_poll`. Verify that
  `tests/test_dated_schedule_ingest.py` passes and that the group test set passes.
- [x] 5.6 Gate: run `t1-S` on a fresh seed-S copy with indexes, and compare it with `ref-S`.
  Verify exit 0, and record the speedup with a `--profile` run, showing that
  `_get_matched_objects_on_criteria` is below 1% of samples.
- [~] 5.7 D6.2: add `replay_day(merge_plan_hours=...)` and `backfill
  --merge-plan-hours/--no-merge-plan-hours`, default off. Run S with the flag
  on; keep the default off until final W1/W3 checks pass, and compare each against its reference with `--allow
  train.n_runs_seen,train.n_stops_tracked`. Apply the D6.2 rule. Verify that
  `$W/phaseC-notes.md` states the decision with the comparison outputs. If the default is
  switched on, also verify that the `models.Train` comments describe the counters' new
  meaning.
- [x] 5.8 Document `--merge-plan-hours` in the README. Verify that the section exists.

## 6. Tier 3 — batched plan writer (D7)

- [x] 6.1 Factor the normalisation into `_normalize_run(...)` in `runs.py`, used by
  `upsert_run`. Verify that the group test set passes, and that a one-day S replay compared
  with the reference gives exit 0.
- [x] 6.2 Implement `src/dbrel/ingest/runs_batch.py` `upsert_runs_batch`, following D7
  steps 1–5:
  - pre-pass;
  - bulk preload;
  - in-order sequential pass;
  - explicit ids;
  - epochs;
  - ordered Core executemany writes;
  - `expire_all` before settle;
  - `counters.batch_epochs`.

  Verify that `uv run python -c "import dbrel.ingest.runs_batch"` succeeds.
- [x] 6.3 Add `import_plan(..., writer=)` and `backfill --plan-writer per_run|batch`
  (default `per_run` until 6.6). Print `batch_epochs` with `--timings`. Verify that
  `--plan-writer batch` replays S without error.
- [x] 6.4 Write `tests/test_runs_batch.py` with every case in D7 "Differential tests",
  including the drift case, comparing the two writers with `ingest_compare` and no
  allow-list. Also assert the same `ValueError` text for an unmapped EVA. Verify that all
  cases pass. Fix `runs_batch.py`, never the expectation. If the per-run behaviour itself is
  wrong, stop and report.
- [x] 6.5 Small-sample gate: run S with `--plan-writer batch` plus the tier 0 and tier 1
  defaults into fresh copies, and compare each against its reference with the 5.7 allow-list.
  Verify exit 0. Run broad W1/W3 equivalence during final validation before delivery. On failure, keep `per_run`, record the first differing keys in
  `$W/phaseD-notes.md`, and fix or report.
- [x] 6.6 Make `batch` the default for `import_plan`, so live `tt-import-plan` and `collect`
  use it too. Verify that `tests/test_collect.py`, every `tests/test_*plan*` and every
  `tests/test_dated_schedule_*` pass.
- [x] 6.7 Document `--plan-writer` in the README, and that live import uses the same writer.
  Verify that the section exists.

## 7. Final scale-up gates: W1/W3, WL, WN and WH

Run frozen-reference and optimized W1/W3 windows once at this stage and compare
strictly. These broad checks are deferred from per-step iteration, not waived.

Deferred W1/W3 batch-writer gates completed on 2026-10-18: both strict comparisons
exit 0, no allow-list, merged hours off. Fresh-seed replay speedups: W1 2.16x,
W3 2.38x. Evidence and memory peaks are in `$W/phaseD-notes.md`; the live-copy
gates below remain pending.

- [x] 7.1 Check that `fuser data/dbrel.sqlite data/dbrel.sqlite-wal` prints nothing and
  `df -h data` shows at least 30 GB free. Make the reflink copies `live-ref-WL`,
  `live-new-WL`, `live-ref-WN`, `live-new-WN` and `live-new-WH`. Verify that each passes
  `pragma quick_check;` (in a capped unit).
- [~] 7.2 On every copy, run `migrate-trip-schedule-schema` with the matching checkout. On
  the `live-new-*` copies only, run `migrate-backfill-indexes` (with `temp_store=FILE`, or the
  memory estimate recorded). Verify the outputs, and record the index build times in
  `$W/progress.md`.
- [~] 7.3 Run WL (`--restart`) as `ref-WL` and `final-WL`, and WN as `ref-WN` and
  `final-WN`, each in a capped unit with `MemoryPeak` recorded. Use `--profile` on `final-WN`.
  Verify that every JSON has its days, and that `ref-WN` is within ±15% of the 26 min 43 s
  baseline. If it is outside that range, note why before going on.
- [~] 7.4 Run `compare_ingest_dbs.py` for `live-ref-WL` against `live-new-WL` (`--dates`
  for the three days) and for `live-ref-WN` against `live-new-WN` (`--dates 2026-09-29`),
  with the 5.7 allow-list, after writing the memory estimate into `$W/memory-budget.md`.
  Verify exit 0 for both.
- [~] 7.5 Run `final-WH` (2026-09-29 .. 2026-10-05) on `live-new-WH`. Verify that no day
  after the first takes more than 1.25× the first (spec: history independence), and that the
  peak stays under the cap.
- [~] 7.6 Run `scripts/summarize_bench.py` into `$W/bench/summary.md`: seconds per day per
  tier and window, speedups, and the projected year. Verify that the WL and WN speedups are
  each ≥ 3×. If either is lower, do not tune the harness or the allow-list; record the
  per-phase breakdown and the profile, and report in 10.5.
- [~] 7.7 Delete every `live-*` copy (with its `-wal` and `-shm`) after 7.4–7.6 are saved.
  Verify with `df -h data`.

## 8. Comparison against the current database (D10)

- [~] 8.1 Write `scripts/compare_with_live.py` (D10), read-only on both databases. Verify
  with a test on two small synthetic databases that it reports matched shares, quality
  counts and delay agreement correctly, with one example of each disagreement kind.
- [~] 8.2 Produce `$W/reports/live-final-W{1,2,3}.md` and `$W/reports/live-ref-W{1,2,3}.md`.
  Verify that each final report equals its reference report apart from timestamps.
- [~] 8.3 Check the matched share of live trips on every date. If it is below 95% on any
  date, stop and report with the files. Otherwise copy the headline numbers, including the
  count of stopless trips from fingerprint drift, into `$W/progress.md`.

## 9. Integration checks

- [~] 9.1 Run the whole suite with `uv run pytest -q`. Verify that it passes. For a test
  already failing at `HEAD`, prove that by running it in `$W/ref`, and record it.
- [~] 9.2 Run one live-style `collect` cycle against a reflink copy, with credentials present
  and `--for` short enough for a single cycle. Verify that it completes, and that its poll,
  plan and finalize counts look the same as a cycle of the reference checkout on another
  copy. This confirms live behaviour is unchanged.
- [~] 9.3 Run `openspec validate speed-up-archive-backfill --strict`. Verify exit 0.

## 10. Report and cleanup

- [~] 10.1 Write `docs/backfill-speed-results.md`:
  - what changed per tier;
  - the bench table (fresh, WL, WN, WH);
  - the projected year and memory peaks;
  - the path-fix outcome;
  - the merge-plan-hours decision;
  - every compare command with its exit code;
  - the live-comparison headline, with the D10 explanation.

  Verify that every number traces to a file under `$W`.
- [~] 10.2 Delete `data/scratch/prefill-test/` (the 2026-10-17 baseline copy and profile)
  and `$W/archive` only after the user agrees. Verify with `df -h data`.
- [~] 10.3 Remove the reference worktree with `git worktree remove $W/ref` only after the
  user has seen the report. Verify with `git worktree list`.
- [~] 10.4 Re-check the live database: its mtime and first-MiB `sha256` match
  `$W/baseline-state.md`, and `git status` shows only intended source, test, script and doc
  changes. Verify both.
- [~] 10.5 Report to the user:
  - the speedup and the projected year;
  - the equivalence status;
  - the live-comparison headline;
  - the defaults that are on;
  - the follow-up commands for the live database (`migrate-trip-schedule-schema`,
    `migrate-backfill-indexes`, then a rebuild), stated as the user's decision.

  Do not commit or push.

## Workflow follow-up

- Leave the changes uncommitted until the user asks. A push to `main` deploys to
  bahnpredict.de.
- Plan the route-fingerprint drift fix, and the repair of the 225,200 stuck trips, as a
  separate change once this one is in. It changes output, so it needs its own baseline.
- Decide on live `collect` finalize (design, Open Questions).
- Archive this change after review.

## Closed 2026-10-18

Tasks marked `[~]` were skipped, not done. The W1/W3 equivalence gates passed (2.16x and
2.38x, identical rows; `docs/backfill-speed-results.md`). Not done: any run on a full-size
live-database copy, the seven-day history check, `compare_with_live.py` and its requirement
(dropped from the spec), the merged-hours check on W1/W3 (5.7, flag stays off), and the
cleanup tasks. Task 10.1 was done in a shorter form.
