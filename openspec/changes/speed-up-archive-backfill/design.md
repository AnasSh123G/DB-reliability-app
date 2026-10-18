# Design

## Context

This design merges two pieces of work:

- **The 2026-10-14 explore and propose session (`845fee87`).** It defined tiers 0–3, profiled
  the plan phase on an empty scratch database, and wrote the harness, safety and tier
  designs. Those are kept almost verbatim below: D1–D7, D9 and D10. Its separate draft was
  deleted on 2026-10-17; this change is the only copy.
- **The 2026-10-17 real-database measurement.** A full day was profiled on a reflink copy of
  the live database, which exposed costs the empty-database profile could not show.

Tier 2 (parse-ahead) is dropped at the user's request. Parsing measured 2% of the time.

### Measured baseline (2026-10-17)

The run:

- Command: `dbrel backfill --from 2026-09-29 --to 2026-09-29`, with code at `00ba96f`.
- Database: a `cp --reflink=always` copy of `data/dbrel.sqlite` (51 GB, last written
  2026-10-07), with `migrate-trip-schedule-schema` applied first.
- Wall time: 18:53:34 → 19:20:17, so **26 min 43 s**.
- Output: 4 sweeps, 130,128 plan buckets, 37,483 stops stored and 181,175 gap rows.
- Resources: one core at about 93%, about 4.5 GB read from disk (mostly database pages), no
  memory pressure (RSS about 0.9 GB early in the run).
- Profile: `py-spy record --idle --rate 50`, raw stacks in
  `data/scratch/prefill-test/profile-20261017T185334.txt`. Main thread: 80,002 samples,
  1,600 s.

| Rank | Where | Seconds | Share | Cause | Fixed by |
|---:|---|---:|---:|---|---|
| 1 | `finalize_tt.py:132`, trip select | 367 | 23% | Plan `SEARCH trip USING INDEX sqlite_autoindex_trip_2 (source=?)` walks 13.5M TT trips to filter `finalized_at IS NULL`. No `sqlite_stat1`. | D5 partial index, D5 replay bound |
| 2 | `runs.py:394`, ORM `update(Trip)` | 355 | 22% | The default `synchronize_session` evaluates the WHERE clause against every identity-map object (323 s in `_get_matched_objects_on_criteria`). The identity map grows all day because one session is used per day. | D6.0; D7 for the plan path |
| 3 | `record_plans` | 254 | 16% | One `station_stop` SELECT per observation (46 s), `refresh_run_bounds` (35 s) and `reconcile_facts` (19 s) per call, 40-row inserts (75 s). | D6.3, D6.4, D7 |
| 4 | Rest of finalize | ~140 | 9% | Loads all 7.3M `tt_poll` rows (39 s); patterns, recorded, reconcile and the gap insert take about 100 s. | D5 bounded polls, D5 replay bound |
| 5 | `changes_poller.py:520`, ORM `update(Trip)` | 56 | 3.5% | Same identity-map evaluation as rank 2. | D6.0 |
| 6 | `_load_trip_index`, per sweep | 52 | 3% | Same planner problem as rank 1. The same count takes 1.08 s through the `source` index and 0.01 s through the date index. | D5 index |
| 7 | Per-call setup in 24 `import_plan` calls | ~40 | 2.5% | Tracked stations, `train_by_key` (250,194 rows) and `tt_plan_fetch` reloaded on every call. | D6.1 |
| 8 | XML parsing | 30 | 2% | Inherent work. | — |

What each source contributes:

- The empty-database profile added the remaining per-run ORM overhead: about 20 statements per
  `upsert_run`, 55,800 INSERT compilations, and a run being upserted once per plan hour it
  appears in. D6 and D7 address it.
- The two ORM-update rows (ranks 2 and 5, 25% together) appear only with a large identity map,
  so the empty-database profile could not show them.

### How a day is replayed today

The call chain is `cli.backfill` → `replay.replay_day(session, store, day, tracked_evas)`. It
uses one `session_scope()` per day, which commits at the end of the day and rolls back on any
exception.

For each partition file of the day (usually 4–5 files):

1. It reads all responses, keeps tracked EVAs, and splits them into `plan_by_hour[hour]` and
   `changes`.
2. For each hour, it calls `import_plan(session, ArchiveReplaySource(...), date=day,
   hours=[hour], force=True, now=max recorded_at)`. Each call:
   - loads every tracked `Station`;
   - loads the day's `tt_plan_fetch`;
   - loads `train_by_key`, all 250k TT train keys;
   - writes one `TtPlanFetch` per tracked station;
   - groups stops by `run_key`;
   - calls `upsert_run` once per run.
3. `upsert_run` (`ingest/runs.py`) issues about 20 statements per run:
   - `session.get(Train)`, then a new `Train` and `SearchAlias` rows if needed;
   - a SELECT of the existing `station_stop` pattern, then an INSERT of missing stops;
   - a SELECT of the `Trip` by (train, date), then an ORM UPDATE (rank 2), or an INSERT ON
     CONFLICT followed by a SELECT of the id;
   - then `record_plans(...)`.
4. `record_plans` (`ingest/dated_schedule.py`):
   - validates each observation;
   - does one SELECT per stop for `station_stop.id`;
   - builds ORM `TripScheduleSnapshot` objects;
   - inserts them 40 at a time as multi-VALUES `ON CONFLICT DO NOTHING RETURNING`, recompiled
     each time;
   - calls `refresh_run_bounds` (a GROUP BY);
   - if anything was inserted, calls `reconcile_facts`.
5. For each `/fchg` sweep, it calls `run_tt_poll(...)`, which:
   - loads tracked stations;
   - runs `_load_trip_index`, which builds run_key → trip for 3 service dates (rank 6);
   - loads `train_by_key` again;
   - per station:
     - parses the response;
     - hashes the body once per full-record stop;
     - calls `record_plans` per full-record stop of a known run, and `upsert_run` for
       unknown runs;
     - runs `_load_patterns` and writes facts;
     - runs the ORM status UPDATE (rank 5).
6. At the end of the day it calls `run_finalize_tt(session, now=last_instant)`. That loads
   every poll, selects unfinalized trips across all history, resolves patterns, reconciles,
   writes gap rows and rolls up.

### Other facts

- **Table sizes** in the live database:

  | Table | Rows |
  |---|---:|
  | `trip` | 13.6M (13,586,436 TT) |
  | `trip_stop` | 122M |
  | `station_stop` | 5.5M |
  | `tt_plan_fetch` | 41.6M |
  | `tt_poll` | 7.27M |

- **Never-finalizable trips.**
  - 241,097 TT trips are unfinalized, and 234,530 of those have no `trip_stop` rows.
  - **225,200** of them are permanently stuck because of route-fingerprint drift. The digest
    is computed from the head stop's own name, taken from our station table (GTFS spelling,
    e.g. "Pforzheim Hauptbahnhof"), plus IRIS's spelling for every other stop
    ("Pforzheim Hbf"). So it depends on which station heads a `/plan` bucket.
  - One run therefore gets two digests. With an operator mismatch that means two `#variant`
    trains.
  - The trip upsert's `ON CONFLICT DO UPDATE` changes `path_sha1` but not `train_id`. The
    pattern lookup finds nothing, so no facts or gap rows are ever written and `finalized_at`
    is never stamped.
  - The other 9,330 were simply cut off when collection stopped on 2026-09-27.
  - Fixing the drift is **out of scope**, because it changes output. But it is the likely
    source of the re-points behind the Phase A error (D4).
- **The live database has no `trip_schedule_snapshot` table.** Any copy needs
  `dbrel migrate-trip-schedule-schema` before replay.
- **The local archive is gone.** `data/archive/` holds only `manifest.json` and
  `replayed.json` (236 KB). Every test window must be fetched again with
  `dbrel archive-fetch --root $W/archive`, at about 0.17 GB per day plus one edge day each
  side.
- **Resume file.** `backfill` writes it to `<root>/replayed.json`. Every scratch run passes
  its own `--root` or `--done-file`.
- **Replay is deterministic.** All "now" values come from the archive's `recorded_at`. The
  only wall-clock columns are `tt_poll.fetch_ms`, `parse_ms` and `store_ms`;
  `feed_version.imported_at` is written only when the TT feed row is created.
- **Machine.** 16 cores, 30 GB of RAM, and swap is zram. `data/` is on btrfs with 221 GB
  free. SQLAlchemy 2.0.52.
- **Connection settings.** `db.py` sets `synchronous=NORMAL`, `temp_store=MEMORY`,
  `cache_size=-131072` (128 MB) and `mmap_size=268435456` (256 MB).
- **The poller's `RunContext.station_id_by_eva`** holds only the stations polled in that
  sweep, a subset that depends on `sweep_index`.
- **Line numbers** cited below are for `00ba96f`. The ingest code is unchanged since
  `e06b2c9`.

## Goals / Non-Goals

**Goals:**

- After optimization the ingest code produces the same database rows (by natural key) as
  before. This is proven on real archive days and checked by a script anyone can re-run.
- End-to-end replay per day is at least 3× faster on copies of the live database. The
  stretch target is a full year in 24 hours or less.
- Per-day cost does not grow with accumulated history.
- Each optimization is a separate step, verified by the harness, so a step that breaks
  equivalence can be reverted on its own.
- Live collection keeps its settings and its output.

**Non-Goals:**

- Rebuilding or modifying the live `data/dbrel.sqlite`. Indexes and `ANALYZE` on the live file
  are a follow-up command the user runs.
- Changing grading rules, quality semantics, identity rules or the schema beyond indexes.
  That includes the route-fingerprint drift fix.
- Tier 2, parse-ahead in a worker process.
- Parallel writers, sharded or per-day databases, or a database other than SQLite.
- Any scheduling or automation.

## Revised execution order — user direction, 2026-10-18

Implement and verify on a small test sample first, then scale to the full database.
Do not run broad multi-day reference windows before starting the speed fixes. The
previous reference coordinator was intentionally stopped; keep its partial evidence.

- S: one real weekday (2026-06-09), with 24 deterministic tracked stations spanning
  the station list, including Bochum and Dormagen. Start both implementations from
  identical copies of a reference-only seed. Use the existing archive without fetching.
- Establish a reference S run and a repeat S run once, compare them strictly, then
  use S plus focused synthetic tests for each optimization gate. No allow list beyond
  the already specified optional merge counter exception.
- Synthetic tests cover path changes, future polls, midnight crossings and DST; use
  larger real windows only after the implementation works on S.
- Apply Tier 0 and D6.0 first, since the measured query scans and ORM synchronization
  are known costs. Follow with the remaining Tier 1 and Tier 3 changes.
- Full-scale validation still uses scratch copies of the live database. Final checks
  cover the DST/gap windows, large database equivalence and throughput; these are not
  prerequisites to implementing the fixes.
- Preserve completed Phase A code and its frozen reference. This revision changes
  sequencing and validation workload, not grading rules, data identity, or tolerances.
- All subagents must use high or lower reasoning effort. Never use max. This user
  preference overrides earlier agent-selection recommendations.

## Decisions

### Safety rules (apply to every step)

1. **The live database is never written.**
   - Every command that writes uses `DBREL_DB=<scratch path>`. Print `DBREL_DB` before each
     run, and stop if it resolves to `data/dbrel.sqlite`.
   - The only allowed operations on the live file are `sqlite3 -readonly` queries,
     `ATTACH ... ?mode=ro`, and `cp --reflink=always`.
   - Before copying, `fuser data/dbrel.sqlite data/dbrel.sqlite-wal` must print nothing.
2. **Workspace:** `W=data/scratch/backfill-speed`. All scratch databases, archive partitions
   (`$W/archive`), logs, patches, resume files and reports go there.
3. **Resume file:** a new option `backfill --done-file PATH`. The default stays
   `<root>/replayed.json`, so live behaviour does not change. Every scratch run passes
   `--root $W/archive --done-file $W/<db-name>.replayed.json`.
4. **Memory and process safety.** Every replay, benchmark or comparison over more than one day
   runs as:

   ```
   systemd-run --user --slice=background.slice -p MemoryMax=<cap> -p MemorySwapMax=0 \
     --unit=dbrel-bfspeed-<step>-<UTC timestamp> --working-directory=<checkout> \
     --setenv=DBREL_DB=<scratch db> <command>
   ```

   - No `--collect`. Output goes to `$W/logs/<unit>.log`.
   - Measure the reference peak for one day first, using
     `systemctl --user show -p MemoryPeak <unit>`.
   - Set `<cap>` to that peak plus the bulk cache size plus 2 GiB, and write the arithmetic
     into `$W/memory-budget.md`. Use 8G for the first measurement only.
   - After any failure, check `journalctl -k | grep -i "out of memory"` first.
5. **Profiling.** py-spy cannot attach to a non-child process (ptrace scope), so profile by
   launching it as the parent: `uvx py-spy record --idle --rate 50 --format raw -o <file> --
   <dbrel command>`. Run with `--rate 50`; at that rate wall time stays comparable.
6. **No commits, no pushes.** The user reviews first, and a push to `main` deploys.

### D1 — Reference implementation = HEAD plus the Phase A patch, in a git worktree

The reference must include the path fix, if one is needed, and the non-behavioural options
`--done-file`, `--timings` and the writer lock. It must not include any optimization.

1. After Phase A is complete and tested, run
   `git diff HEAD -- src tests scripts > $W/phaseA.patch`.
2. `git worktree add --detach $W/ref HEAD`, then `git -C $W/ref apply $W/phaseA.patch`.
3. Inside `$W/ref`, run `uv sync --frozen --extra backfill`.
4. Reference runs use `uv run --directory $W/ref dbrel ...`. Optimized runs use
   `uv run --directory <repo> dbrel ...`.
5. Check that `uv run --directory $W/ref python -c "import dbrel; print(dbrel.__file__)"`
   points into `$W/ref`.

**Rejected:** comparing directly against the live database. It was built by older code
(before `03d834a`) and partly by 15-minute live collection, so differences there do not
isolate the optimization. The live comparison is still produced, as a report (D10).

### D2 — Scratch databases and sample windows

`scripts/seed_scratch_db.py --live data/dbrel.sqlite --out <path>` creates a fresh scratch
database:

1. Refuse if `<path>` exists or resolves to the live file.
2. With `DBREL_DB=<path>`, call `dbrel.db.create_all()`. This creates every table, including
   `trip_schedule_snapshot`, plus the views.
3. `ATTACH 'file:<live>?mode=ro' AS live` and copy only the reference tables: `station`,
   `feed_version`, `route` and `public_holiday`.
   - Use explicit column lists: the intersection of `PRAGMA main.table_info(t)` and
     `PRAGMA live.table_info(t)`.
   - Keep ids as they are.
   - Copy all `station` rows with `tracked` and `poll_priority` unchanged.
   - Reset ingest-written station fields: `last_plan_hour = NULL`, plus every other field
     `models.Station` shows replay writes (`last_fchg_at`, `n_fchg_polls`, `n_fchg_errors`).
     List them in the script's docstring.
4. Print row counts per copied table.

Both implementations start from byte-identical seeds: create one seed, then
`cp --reflink=always` it to `<name>-ref.sqlite` and `<name>-new.sqlite`.

Windows. Days are replayed in order, and each window gets its own seed or copy.

| Window | Dates | Database | Why |
|---|---|---|---|
| W1 | 2026-03-28 .. 2026-03-30 | fresh seed | DST change on 2026-03-29; that day's partitions start at hour 01 |
| W2 | 2026-06-09 .. 2026-06-11 | fresh seed | ordinary Tue–Thu, new partition naming. Final broad ordinary-day coverage; S is the per-step check. |
| W3 | 2026-08-27 .. 2026-08-29 | fresh seed | archive hole: hours 05–07 of 2026-08-28 are missing |
| WL | 2026-09-08 .. 2026-09-10 | two reflink copies of live, `--restart` | large database, days already covered |
| WN | 2026-09-29 | two reflink copies of live | large database, a **new** day; the measured 26 min 43 s baseline |
| WH | 2026-09-29 .. 2026-10-05 | one reflink copy of live, optimized code only | history independence (spec: consecutive days within 1.25×) |

Archive acquisition verification (2026-10-17): the user approved retaining the sample
windows with additional mirror gaps. W1 lacks midnight on March 29 and March 30; W3 lacks
August 27 05–06 as well as August 28 05–07. These gaps are recorded, not synthesized; the
row-equivalence gates remain unchanged.

Archive acquisition is one `dbrel archive-fetch --root $W/archive --from <first> --to
<last>` per window. That is about 20 distinct days plus edges, roughly 4 GB. Check
`df -h data` first and require at least 30 GB free.

### D3 — Equivalence harness: compare by natural key, as multisets

The logic lives in `src/dbrel/ingest_compare.py`, so tests can import it.
`scripts/compare_ingest_dbs.py A B [--dates D1,D2,...] [--allow table.col,...] [--samples 5]
[--json out.json]` is a thin wrapper. It opens both databases read-only with plain `sqlite3`
(`file:...?mode=ro`, `uri=True`).

For each compared table, build a row tuple in which:

- the surrogate `id` is dropped;
- every foreign key is replaced by the referenced row's natural key;
- allowed and timing columns are dropped.

Then compare `collections.Counter`s of tuples grouped by natural key. Report per table:
`rows_a`, `rows_b`, `only_a`, `only_b` and `differing` (same key, different values). For
`differing`, report the differing columns with up to `--samples` examples.

| Table | Natural key | FK replacements / dropped columns |
|---|---|---|
| `feed_version` | `(kind, source_url, content_sha256)` | — |
| `station` | `id` (seeded identically) | none dropped |
| `service_line` | `identity_key` | — |
| `train` | `identity_key` | `line_id` → service_line natural key; `route_id` kept (seeded); `*_feed_version_id` → feed_version natural key |
| `search_alias` | `(alias_norm, kind, target)` with target `('T', train key)`, `('L', line key)` or `('S', station_id)` | — |
| `station_stop` | `(feed_version key, train key, path_sha1, stop_sequence)` | — |
| `trip` | `(source, source_run_key, service_date)` | `train_id` → train key; `feed_version_id` → feed_version key |
| `trip_stop` | `(trip key, stop_sequence)` | `station_stop_id` → station_stop key |
| `trip_schedule_snapshot` | `(trip key, stop_sequence, observed_at, source_content_sha256, value_fingerprint)` | `station_stop_id` → station_stop key |
| `tt_message` | `(trip key, stop_sequence, msg_id, ts)` | — |
| `tt_poll` | `(sweep_id, eva)` | drop `fetch_ms`, `parse_ms`, `store_ms` |
| `tt_plan_fetch` | `(eva, plan_date, plan_hour)` | — |
| `tt_unmatched` | `(service_date, daily_trip_id, stop_index)` | `first_poll_id` / `last_poll_id` → tt_poll key |

Rules:

- **Columns** are discovered with `PRAGMA table_info`, so new columns are compared
  automatically. Only the columns named above and in `--allow` are dropped.
- **`--allow`** is empty by default. Only per-partition plan import (D6.2) may add exactly
  `train.n_runs_seen,train.n_stops_tracked`.
- **`--dates` restricts:**
  - trip-keyed tables to trips whose `service_date` is in the dates;
  - `train`, `station_stop` and `search_alias` to trains referenced by those trips;
  - `tt_plan_fetch` and `tt_unmatched` by their date columns;
  - `tt_poll` to rows whose `fetched_at` falls between the first and last day's local
    midnights (Europe/Berlin), padded by one day on each side.
  - It also compares every trip, in any date, whose `finalized_at` differs between the two
    databases. This catches a finalize-bound regression (D5) outside the window.
- **Missing tables:** a table that exists in only one database is reported as a difference.
  Tables not listed (the aggregates, `passenger_line*`, `notice_rate_daily`) are listed as
  "not compared".
- **Exit codes:** 0 identical, 1 differences, 2 usage error.
- **Large databases:** stream rows per table ordered by natural key and compare in chunks of
  about 200k keys. Never hold 122M `trip_stop` tuples. Estimate the peak before a WL or WN
  comparison and record it in `$W/memory-budget.md`.
- **Self-test (unit test):** copy a tiny database, change one `trip_stop.dep_delay_s`, and
  assert exit 1 with that table, key and column named. Delete one snapshot row and assert
  `only_a == 1`.

D3 implementation correction (independent review, 2026-10-17): feed-version keys include
`content_sha256`. `(kind, source_url)` alone is not unique across GTFS downloads and hides
foreign-key changes between versions. Including the content hash strengthens the equality
gate; it does not allow any additional row differences. Missing referenced tables must produce
a structured schema difference rather than abort dependent joins. Report output must reject
input database and WAL/SHM aliases, including hard links and both supplied/canonical database spellings, before opening either input.

### D4 — Phase A: reproduce the path mismatch, then fix it

The 2026-10-14 scratch run died with `ValueError: path does not match trip` in the first
`/fchg` sweep, on a database with invented stations. The 2026-10-17 real-database replay of
2026-09-29 did **not** raise it. Both data points go into `$W/phaseA-notes.md`.

**Reproduce first.** Replay W2 with HEAD code, plus only the Phase A options, into a fresh
seed. If W2 passes, try W1 and W3. If the error appears, record the full traceback. The
caller tells which candidate it is:

- **Candidate 1 (expected): `changes_poller.py:348`.**
  - `trip_by_run` is loaded at sweep start.
  - Later in the sweep, `upsert_run(...)` for an unknown run resolves to an existing trip of
    the same train and date (`runs.py` `existing_id` branch) and changes its `path_sha1`.
  - Every other cached `_TripRef` for that trip keeps the old path, so the next
    `record_plans(..., ref.path_sha1, ...)` raises.
  - The same staleness makes `_load_patterns` silently skip the trip.
  - The fingerprint drift (Context) is the likely way a run's digest changes mid-day.
- **Candidate 2: `runs.py:406` or `runs.py:446`.** The ORM UPDATE did not synchronise the
  identity-map `Trip`. D6.0 replaces that UPDATE with an identity-map assignment, which
  removes this failure mode. If the traceback shows candidate 2, record it, apply D6.0 early
  as part of Phase A, and re-run. Report to the user only if it still fails.

**Fix for candidate 1, in `changes_poller.run_tt_poll`:**

- Keep `run_keys_by_trip: dict[int, set[str]]`, built from `trip_by_run` at sweep start and
  updated whenever a ref is stored.
- Add `_refresh_refs(session, trip_id)`. It reads `session.get(Trip, trip_id)` and rewrites
  every `trip_by_run[k]` for `k in run_keys_by_trip[trip_id]` with the current `train_id`,
  `service_date` and `path_sha1`.
- Call it right after every `upsert_run(...)` in the poller.
- Before `record_plans` for a known ref, re-read the trip's current `path_sha1`. If it
  differs, refresh the refs and use the refreshed one.
- Build `dates_by_train` for `_load_patterns` from each realized ref's current trip path.
- Count `PollResult.refs_refreshed` and print it with `--timings`.

Regression test `tests/test_backfill_speed_pathfix.py`, from synthetic XML in the style of
`test_archive_replay.py`:

- a plan creates run R1 of train T, date D, path P1;
- a `/fchg` full record for run key R2 (another `daily_trip_id`, same train number and date)
  has digest P2 and resolves to the same trip;
- then a full record for R1 arrives in the same sweep.

Assert that no exception is raised, the snapshot is stored under P2, and R1's realized stop
produces its fact. First confirm by reading `upsert_run` that this sequence reaches the
`existing_id` branch. If it does not, build the fixture from the real traceback's data.

If no window reproduces the error, leave the poller unchanged, record "not reproduced", and
continue.

**Also in Phase A** (non-behavioural, needed by the reference):

- `backfill --done-file PATH`.
- `backfill --timings`, which prints one line per day:
  `timings <date> plan_s=… sweeps_s=… finalize_s=… total_s=… upsert_calls=…
  refs_refreshed=…`. Use `time.perf_counter` around the three phases in `replay_day`, on new
  `ReplayResult` fields.
- **The writer lock:**
  - always `exclusive_lock(writer_lock_path(DB_PATH), label="backfill")`, where the shared helper resolves the database path before adding `.writer.lock`;
  - additionally `TT_SWEEP_LOCK` when `DB_PATH` resolves to the default live database;
  - `AlreadyRunning` is reported the same way `collect` reports it.

  Both `collect` and `backfill` acquire this per-database lock first, before any sweep
  lock, including for a custom `DBREL_DB`. This closes the independent review's custom-path
  and symlink exclusion gaps. Resume destinations must reject active/default databases and
  their WAL/SHM files, and writer/sweep lock paths, including filesystem aliases,
  before any replay. Lock setup must reject symbolic links and descriptors with more than
  one hard link before writing metadata, preserving existing lock inodes and exclusion.
  Contention reporting tolerates non-object JSON holder metadata.

  The lock is required before D7's explicit ids.

### D5 — Tier 0: indexes, ANALYZE, bounded finalize, bulk pragmas only during backfill

**Indexes**, declared in `models.py` so `create_all` makes them on new databases:

| Index | Definition | Serves |
|---|---|---|
| `ix_trip_src_date` | `trip(source, service_date)` | `_load_trip_index` |
| `ix_trip_unfinalized` | `trip(source, service_date) WHERE finalized_at IS NULL` (partial, `sqlite_where=text("finalized_at IS NULL")`) | the finalize trip query |
| `ix_ttpoll_ok_time` | `tt_poll(fetched_at) WHERE error IS NULL AND http_status = 200` | the bounded poll load |
| `ix_ttpoll_ok_station_time` | `tt_poll(station_id, fetched_at) WHERE error IS NULL AND http_status = 200` | latest poll per station |

**New command `dbrel migrate-backfill-indexes`:**

- runs `CREATE INDEX IF NOT EXISTS` for the four indexes, then `ANALYZE`, printing the
  elapsed time per statement;
- is run on scratch databases and reflink copies only. Running it on the live database is the
  user's later decision, documented in the README;
- sorts in temp storage. Before running it on a live copy, set `PRAGMA temp_store=FILE` on
  that connection, or record the estimate of 13.6M × about 40 B ≈ 0.6 GB.

**Query changes:**

- **`_load_trip_index`:** keep the SQL. With the index and `ANALYZE`, the planner should use
  `ix_trip_src_date`.
  - Test: build the schema, run `ANALYZE` on a small seeded database, and assert that
    `EXPLAIN QUERY PLAN` names the index.
  - If the planner still picks the autoindex, add
    `.with_hint(Trip, "INDEXED BY ix_trip_src_date", "sqlite")`. The fallback measured on
    2026-10-17, `+trip.source` (0.01 s against 1.08 s), is acceptable too.
  - Keep the test either way.
- **Finalize trip query:** unchanged SQL. Assert with `EXPLAIN QUERY PLAN` that
  `ix_trip_unfinalized` is used.
- **Replay finalization retains the unbounded eligible-trip query.** Review C1 on a
  two-station fixture disproved the D-1/D assumption: missing archive days and valid
  plan-only days can leave older trips eligible only on a later sweep. Any fixed widening
  still fails on longer gaps. The date cutoff is deferred; indexes and bounded poll loading
  remain. A future backlog optimization must preserve overdue trips across arbitrary gaps
  before claiming independence from unfinalized history.
  - Keep the optional explicit `service_dates` finalizer API for callers requesting that
    subset; replay itself must not use it.
  - Regressions cover missing-day, plan-only and multi-day gaps against reference facts.
- **`_poll_times_by_station` becomes `_poll_times_by_station(session, since)`:**
  - it loads only rows with `fetched_at >= since`, with the same error and status filter,
    ordered by `fetched_at`;
  - it separately loads `latest_by_station` (`SELECT station_id, MAX(fetched_at) ... GROUP
    BY station_id`, same filter);
  - `since` is the minimum over every `reference + GRACE_S` the finalize loop will ask about.
    Compute it after `pattern` is resolved and before the loop, iterating the same trips and
    stops and skipping recorded sequences exactly as the loop does. If there are none, skip
    loading polls;
  - in the loop, `latest = station_polls[-1]` becomes `latest_by_station.get(id)`;
  - this is exactly equivalent: `_polled_between` only looks at polls `>= start`, and
    `latest` was always the global maximum.

**Bulk pragmas:**

- `db.py` gains a module flag `_BULK_LOAD = False` and `enable_bulk_load()`. The function
  sets the flag and calls `dispose_engine()`, so new connections pick it up.
- The connect listener applies the normal pragmas and then, only when the flag is set:
  - `cache_size=-<BACKFILL_CACHE_MIB*1024>`, default 2048 MiB, from
    `config.BACKFILL_CACHE_MIB`, overridable by env `DBREL_BACKFILL_CACHE_MIB`;
  - `mmap_size=<BACKFILL_MMAP_MIB MiB>`, default 8192;
  - `wal_autocheckpoint=20000`.
- `synchronous` stays `NORMAL`.
- `cli.backfill` gains `--bulk-pragmas/--no-bulk-pragmas` (default on) and calls
  `enable_bulk_load()` before opening any session. Nothing else calls it.
- Test: a normal connection reports `cache_size = -131072` and `mmap_size = 268435456`.
  After `enable_bulk_load()`, a new connection reports the bulk values. A fixture resets the
  flag.

### D6 — Tier 1: same code path, less overhead

**D6.0 — Trip updates through the identity map, not ORM bulk `UPDATE`** (2026-10-17, ranks 2
and 5):

- `runs.upsert_run`, `existing_id` branch: replace
  `session.execute(update(Trip).where(Trip.id == existing_id).values(...))` with
  `trip = session.get(Trip, existing_id)` and attribute assignment of the same four fields.
  Usually an identity-map hit, so O(1).
- `changes_poller.run_tt_poll`, status and poll-count update:
  - execute it as a Core statement:
    `session.execute(update(Trip.__table__).where(...).values(...))`, so ORM
    synchronisation does not run;
  - then expire only the affected trips present in the identity map. Iterate the ids, call
    `session.identity_map.get(session.identity_key(Trip, (trip_id,)))`, and
    `session.expire(obj, ["status", "n_polls_seen"])` when present.
- **Rejected:** `synchronize_session=False` on the ORM statement alone. `record_plans` reads
  `trip.path_sha1` from the in-session object immediately afterwards and would raise
  `path does not match trip` on a stale value. `"fetch"` is correct but adds a round trip per
  call.
- Test:
  - `upsert_run` returning on the `existing_id` branch leaves the in-session `Trip` with the
    new `path_sha1`;
  - two polls of the same station in one session read back the correct `n_polls_seen` and
    `status`;
  - with a `before_cursor_execute` counter, no statement scans the identity map. Assert on
    profile or timing indirectly: `_get_matched_objects_on_criteria` must not be called.
    Monkeypatch it to raise in the test.

**D6.1 — Day-scoped shared context:**

- A new dataclass `ReplayDayContext` in `replay.py` holds `fv_id`, `train_by_key`,
  `line_cache` and `tracked_stations`. `tracked_stations` is a list of `Station` ORM objects
  loaded once per day in the day's session.
- `import_plan` and `run_tt_poll` gain `day_ctx=None`. When it is given, they use its
  `train_by_key`, `line_cache`, `fv_id`, and, in `import_plan`, `tracked_stations`, instead of
  querying. When it is `None`, live behaviour is unchanged.
- In `run_tt_poll`, `station_id_by_eva` and `station_name_by_eva` must still come from the
  per-sweep filtered stations.
- `RunContext.train_by_key` must be the same dict object as `day_ctx.train_by_key`, so trains
  created in one call are visible to the next. `upsert_run` already writes new identities
  into it.
- Build the context once per day inside the day's `session_scope`. Never reuse it across
  days.

**D6.2 — One `import_plan` per partition** (flagged, default off until the gate passes):

- `replay_day(..., merge_plan_hours=False)`. When `True`, it builds one
  `ArchiveReplaySource` from every plan response of the partition and calls `import_plan`
  once with `hours=sorted(plan_by_hour)` and `now=max(recorded_at)`.
- **Expected differences:** `train.n_runs_seen` counts calls and `train.n_stops_tracked` is
  the maximum stops per call, so both change.
- **Real risk, raised on both 10-14 and 10-17.** Merging hours groups stops into runs
  differently:
  - the pinned station check sees different earlier stops;
  - with the fingerprint drift, the head stop and therefore the digest can differ.

  Either changes `station_stop`, `train` and `trip` rows.
- **Rule:** run W1, W2 and W3 with `--allow train.n_runs_seen,train.n_stops_tracked`.
  - If those are the only differences, make the default `True` and document the counters'
    changed meaning in the `models.Train` comments.
  - Otherwise keep it `False`, record the differing tables and counts in
    `$W/phaseC-notes.md`, and leave the flag.
  - Never change `upsert_run`'s pinning or digest logic to make them match. That would change
    live behaviour.

**D6.3 — Deferred settling of bounds and reconciliation, split at re-points:**

- A new class `PendingSettle` in `schedule_reconcile.py`:
  - `dirty: dict[int, set[int]]` maps trip_id to inserted sequences, possibly empty;
  - `add(trip_id, sequences)`;
  - `settle(session, trip_ids=None)` runs `refresh_run_bounds` for each affected trip, then
    `reconcile_facts` when its sequences are non-empty, and clears it.
- `record_plans(..., pending=None)`: when given, it calls
  `pending.add(trip.id, set(inserted_sequences))` instead of settling immediately.
- `upsert_run(..., pending=None)` passes it through. In the `existing_id` branch, **before**
  the trip's `path_sha1` is changed, it calls `pending.settle(session, [existing_id])` if
  that trip is dirty and its current `path_sha1 != digest`.
- `import_plan` creates one `PendingSettle` when `day_ctx` is given, passes it to every
  `upsert_run`, and settles after the loop, before the final `flush`.
- The poller keeps immediate settling, because it reads facts later in the same station loop.
- **Why this is equivalent:**
  - Bounds come from all snapshots of the current path, so the last call wins, and one settle
    at the end of the epoch gives the same result.
  - The values `upsert_run` writes before a refresh are overwritten by the refresh, unless the
    trip has no snapshot groups. In that case the last call's values remain in both
    implementations, because the writes still happen in order.
  - Reconciling a sequence depends only on that sequence's snapshots under the trip's path.
    Every new snapshot puts the sequence in the dirty set.

**D6.4 — `record_plans` internals:**

- Load all `station_stop` ids for (train, fv, path) with **one** query per call, as a dict
  `(stop_sequence, station_id) -> id`.
- Build plain dicts, not `TripScheduleSnapshot` objects. Keep the same keys and values,
  including the lower-cased sha.
- Use a module-level statement:
  `_SNAPSHOT_INSERT = sqlite_insert(TripScheduleSnapshot).on_conflict_do_nothing(
  index_elements=[...identity...]).returning(TripScheduleSnapshot.stop_sequence)`, executed
  as `session.execute(_SNAPSHOT_INSERT, rows)` (executemany with RETURNING, which SQLAlchemy
  2.0.52 supports on SQLite through insertmanyvalues). If it raises, fall back to the 40-row
  chunks while still building dicts, and note it.
- Keep every validation and the fingerprint calculation exactly as they are, in the same
  order, with the same messages.
- Reuse the caller's `Trip` where the caller already holds it, instead of `session.get`.

**D6.5 — Small items:**

- In `upsert_run`, replace `session.execute(insert(StationStop), ss_rows)` with a
  module-level statement.
- In `run_tt_poll`, compute `hashlib.sha256(resp.body).hexdigest()` once per station. It is
  already computed for `poll.content_sha256`. Reuse it for every `PlanReceipt` instead of
  re-hashing the whole body per full-record stop.

### D7 — Tier 3: batched plan writer, `upsert_runs_batch`, inside `import_plan`

The new module `src/dbrel/ingest/runs_batch.py` provides
`upsert_runs_batch(session, ctx, counters, runs, *, pending)`. `runs` is a list of
`(run_key, stops, observations)` **in the same order** `import_plan` iterates `by_run`. The
result must be the same database as calling `upsert_run` for each entry in order.

1. **Pre-pass (pure Python).**
   - For each run, do the same normalisation as `upsert_run`: copies, `fix_dst`, normalised
     observations, `head`, `full_path`, `digest`, `identity`, and the line
     `(category, line, owner)`.
   - Factor that part of `upsert_run` into `_normalize_run(ctx, counters, stops,
     observations)` and call it from both, so it exists once. That keeps the fingerprint
     drift identical: the batch must reproduce it, not fix it.
2. **Bulk preload** (chunks of 900 ids):
   - `service_line` by `identity_key` for keys not in `line_cache`;
   - `Train` rows (`id`, `path_sha1`, `operator`, `last_seen_date`, `n_runs_seen`,
     `n_stops_tracked`) for every train id `train_by_key` maps any base or `#digest` variant
     identity to;
   - `StationStop (train_id, path_sha1, stop_sequence, station_id, id)` for those trains and
     `feed_version_id = fv_id`;
   - `Trip (id, train_id, service_date, source_run_key, path_sha1, feed_version_id, source)`
     for `service_date IN (dates of the batch)`, indexed by `(train_id, service_date)` and by
     `(source, source_run_key, service_date)`.
3. **Sequential pass (pure Python, in order).** Replicate `upsert_run` against in-memory
   state:
   - **Train identity:** apply the variant rule exactly. A conflict needs `path_sha1` to
     differ **and** `operator` to be set and differ. Update counters identically.
   - **New trains:** allocate explicit ids from `max(train.id) + 1`, read once. Create the
     `Train` dict with every field `upsert_run` sets, plus the alias rows. Add the train to
     `train_by_key`.
   - **Existing trains:** apply the same `last_seen_date`, `n_runs_seen` and
     `n_stops_tracked` updates in memory. The last value is written once.
   - **`station_stop`:** take the existing rows for `(train_id, digest)` as of the start of
     this run, and apply the pinned check exactly. After the run, append the new rows so
     later runs see them.
   - **Trips:** look up `(train_id, service_date)`.
     - If found, record an update of `supposed_departure_utc`, `supposed_arrival_utc`,
       `n_stops_tracked` and `path_sha1 = digest`.
     - Otherwise look up `(SOURCE_TT, run_key, service_date)`. If found, record the same
       update; the ON CONFLICT case keeps its `train_id`, which is the drift behaviour and is
       preserved deliberately.
     - Otherwise allocate an explicit trip id and record an insert.
     - Before applying a path change to a trip that is dirty in `pending`, or that has
       buffered snapshot rows, **end the epoch**.
   - **Snapshots:** run `record_plans`'s validation and row building against the in-memory
     trip; the path check raises the same `ValueError`. Resolve `station_stop_id` from the
     in-memory map using the reference's key `(train_id, fv_id, path_sha1, stop_sequence,
     station_id)`. It is `None` when the trip's `feed_version_id` is `None`. Buffer the rows.
4. **Flushing an epoch.** Write with executemany and module-level statements, in this order:
   1. `service_line` inserts;
   2. `train` inserts;
   3. `search_alias` inserts;
   4. `train` updates (`bindparam`, executemany);
   5. `station_stop` inserts;
   6. `trip` inserts;
   7. `trip` updates;
   8. snapshot inserts (`ON CONFLICT DO NOTHING RETURNING trip_id, stop_sequence`), then
      `pending.add` for each returned pair, plus an empty add for every trip with
      observations, because the reference always refreshes bounds;
   9. `session.flush()`, `session.expire_all()`, then `pending.settle(session)`. Expire before
      settling, because `refresh_run_bounds` and `reconcile_facts` read ORM attributes. If
      `expire_all` proves costly, expire only `Train` and `Trip` instances.

   An epoch ends at the end of the batch, or before a re-point of a dirty trip. Count epochs
   in `counters.batch_epochs` and print the count with `--timings`.
5. **Explicit ids** are safe only with a single writer, which is why D4 adds the lock.
6. **Wiring:**
   - `import_plan(..., writer="per_run")`. `"batch"` calls `upsert_runs_batch` with the
     ordered `by_run` items.
   - `backfill --plan-writer [per_run|batch]`.
   - The default becomes `batch` only after the gate passes. Live `tt-import-plan` and
     `collect` use the same default, so live and backfill keep one ingest path.
   - The poller keeps `upsert_run`, with D6.0 applied, for single-stop additions.
7. **Differential tests** (`tests/test_runs_batch.py`). One synthetic `import_plan` input
   covering:
   - a brand-new train;
   - an existing train on a new date;
   - an existing trip on the same date under a different run key;
   - a numbered train with a different path and operator (the variant);
   - the same number and path with a different operator (no variant);
   - a station visited twice (the pinned check);
   - a duplicate observation;
   - an observation with neither arrival nor departure;
   - a DST-ambiguous stop;
   - a trip re-pointed mid-batch while dirty;
   - a trip with existing facts, so reconcile runs;
   - **a run whose head stop name differs from the IRIS spelling** (the drift case), so both
     writers produce the same drifted digests.

   Run it into two in-memory databases, `per_run` and `batch`, and compare with
   `ingest_compare` with no allow-list. They must be identical. Also assert the same
   `ValueError` text for an unmapped EVA.

### D8 — Tier 2 is not part of this change

The 2026-10-14 plan's parse-ahead worker (`prepare_partition`, `Response.parsed`, picklable
parser results) is dropped. Parsing measured 30 s of 1,600 s. With one SQLite writer and a
writer-bound pipeline, overlapping it would save at most that.

### D9 — Benchmark

`scripts/bench_backfill.py --checkout <dir> --db <scratch> --from D --to D --label <name>
[--root $W/archive] [--extra-args "..."] [--profile]`:

- It refuses a live database path. Its default `W` is anchored to the repository owning
  the benchmark script, independent of caller CWD; the selected checkout controls only
  the replay process CWD. Print resolved `W` and `DBREL_DB` before launch.
- It runs `uv run --directory <checkout> dbrel backfill --from --to --restart --timings
  --root <root> --done-file $W/<label>.replayed.json <extra>` with `DBREL_DB` set. It is
  itself launched under `systemd-run`, per the safety rules.
- With `--profile`, it wraps the run in py-spy as in safety rule 5, writing
  `$W/bench/<label>.pyspy.txt`.
- The profiled process runs from the requested checkout, like `uv --directory` in ordinary
  mode. A small same-process console-script wrapper records the replay's own exit status
  with an invocation token. The sampler's exit code alone cannot certify success: py-spy
  0.4.2 can exit zero when replay exits nonzero. Accept only a fresh matching replay status
  of zero, sampler status zero, and exactly one valid timing per requested date.
- Before launch, remove the validated scratch resume file. Accept a result only when the
  child writes a fresh list containing exactly the requested ISO service dates, without
  duplicates. A no-partition CLI run can otherwise exit zero and print a zero timing line
  without replaying anything. A processed empty-service day remains acceptable.
- Live-path protection includes persistent custom `DBREL_DB` settings from project `.env`
  files and inherited custom live paths. The explicit inherited scratch override used by
  capped commands is permitted only for the exact canonical target inside `W`, and never
  overrides protection for a persistent live identity. Protect database companions and locks
  against output aliases too.
- It parses the `--timings` lines into `$W/bench/<label>.json`. `--memory-peak` lets the
  operator add the unit's `MemoryPeak` afterwards.

`scripts/summarize_bench.py` reads `bench/*.json` and writes a table with, per window and per
tier:

- seconds per day;
- speedup against the reference;
- the projected year, as mean seconds per day on WL and WN × 332 / 3600 h.

| Label | Database | What |
|---|---|---|
| `ref-W2` | fresh | reference |
| `t0-W2`, `t1-W2`, `t3-W2` | fresh | cumulative tiers, in implementation order |
| `ref-WL`, `final-WL` | reflink copies of live | WL, the reference and the final code |
| `ref-WN`, `final-WN` | reflink copies of live | 2026-09-29; `ref-WN` should land near the 26 min 43 s baseline |
| `final-WH` | reflink copy of live | 7 consecutive new days, the history-independence check |

Each live copy is used once. Make new copies for reruns, and delete old ones after recording
results.

**Projection, not a measurement.** On WN's profile, tier 0 (finalize bound, polls, trip index)
and D6.0 alone remove about 380 + 360 + 90 ≈ 830 s of 1,600 s, which already gives about 2×.
The 10-14 session estimated 3–6× for tier 1 and close to its 10–30× lower bound for a full
bulk loader. Tier 3 batches the plan phase only, so expect it between those. The ≥3× gate is
on measured numbers after implementation; per-step iteration uses S.

### D10 — Comparison report against the current database

`scripts/compare_with_live.py --live data/dbrel.sqlite --scratch <db> --dates D1,D2,... --out
$W/reports/live-<name>.md` is read-only on both databases. Per date, it reports:

- trips on each side, matched by `(source, source_run_key, service_date)`;
- `trip_stop` rows per quality on each side;
- matched stops, by trip natural key and `stop_sequence`;
- for matched stops that are `RECORDED` on both sides, the share with identical `dep_delay_s`
  and `arr_delay_s`;
- counts plus five examples each for: missing in scratch, extra in scratch, quality changed,
  and delay changed.

It is produced for the final-code and reference rebuilds of W1, W2 and W3. The two reports
must be identical apart from timestamps, because D3 requires equality. That is the evidence
that any difference from the live database comes from the current rules (`03d834a`, or the
live 15-minute polling of the September days), not from this change.

There is no pass/fail threshold. **Stop and report** if fewer than 95% of live trips on any
sample date are matched. The fingerprint drift will show up in this report as trips with no
stops on one side; quote its count.

## Risks / Trade-offs

- **[The batch writer drifts from `upsert_run` semantics]**
  - Mitigation: the shared `_normalize_run`, the in-order sequential pass, differential tests
    covering every branch including the drift case, and the harness on four windows.
  - If the gate fails and the cause is not found, ship with `--plan-writer per_run` as the
    default and report.
- **[D6.0 leaves a stale in-session `Trip`]**
  - Mitigation: assignment through the identity map for `upsert_run`, and targeted
    `expire` after the poller's Core update.
  - The harness catches any stale value that is flushed.
- **[Older eligible trips are silently skipped after archive gaps]**
  - C1 reproduced this on a tiny fixture. Replay retains unbounded eligible-trip selection;
    fixed date cutoffs are prohibited without an equivalent overdue-backlog path.
- **[Explicit ids collide with a concurrent writer]**
  - Mitigation: the per-database writer lock, plus the sweep lock for the live database. Ids
    are read inside the day's transaction.
- **[The bulk cache and mmap push the desktop into swap]**
  - Mitigation: defaults of 2 GiB and 8 GiB, units with `MemorySwapMax=0`, and a cap
    derived from the measured peak.
- **[Per-partition plan import changes data]**
  - Mitigation: it is behind a flag and gated with a two-column allow-list. Otherwise it
    stays off.
- **[A harness bug hides differences]**
  - Mitigation: the mutation self-test, plus row counts per table compared independently of
    keys.
- **[The plan hint stops working after a SQLite upgrade]**
  - Mitigation: the `EXPLAIN QUERY PLAN` tests fail loudly.
- **[The speed target is not reached]**
  - Mitigation: report measured numbers honestly with per-phase timings and a profile. Never
    loosen the harness or the allow-list to make a step pass.
- **[Reflink copies and archive downloads fill the disk]**
  - Mitigation: check `df -h data` before each copy (at least 30 GB free), delete each copy
    after its comparison is saved, and delete `$W/archive` at the end, with the user's
    consent.

## Migration Plan

1. Everything is code plus additive indexes. New databases get the indexes from `create_all`.
2. The live database is untouched. After review, the user may:
   - take a reflink backup;
   - run `migrate-trip-schedule-schema` and `migrate-backfill-indexes` on the live file;
   - then rebuild.

   That is a separate decision.
3. **Rollback:**
   - `--plan-writer per_run`, `--no-merge-plan-hours` and `--no-bulk-pragmas` turn each
     optimization off without a code change;
   - indexes can be dropped;
   - D6.0, the finalize changes and the path fix are behaviour-preserving and stay.

## Open Questions

- Should live `collect` finalize also get a service-date bound, or should the never-closing
  trips be stamped closed? With the partial index its scan becomes cheap, but it still reloads
  about 225k stuck trips per finalize. Fixing the fingerprint drift and repairing those trips
  would remove most of them. Both are follow-ups that change live behaviour; this change does
  not depend on the answer.
- Should `n_runs_seen` count distinct service dates instead of calls? This only matters if
  per-partition plan import is enabled. Leave it as it is and mention it in the report.
