# Tasks

**Operational note (Anas, 2026-09-23):** whenever a subagent/background task is running as part
of working on this change and its output is still pending, state an estimate of how long it
should take, not just that it's running.

**Operational note (Anas, 2026-09-23):** for every task worked on here, first check whether the
current model is more than the task needs, or whether it's worth spinning up a subagent to do
it instead — don't default to doing everything inline on whatever model is currently running.

## 1. Make the observation instant injectable

This group must land first and on its own: every later task depends on it, and it is the
only group that touches live code paths. See design.md — Decision 2.

- [x] 1.1 Add `now: int | None = None` to `run_tt_poll()` in `changes_poller.py`, resolving to `int(time.time())` when omitted, and thread it to `RunContext(now=…)`, `_has_happened()`, and the `TtPoll` sweep record. Verify `tests/test_collect.py` and `tests/test_upsert_run_collision.py` still pass unchanged.
- [x] 1.2 Add the same `now` parameter to `import_plan()` in `plan_import.py`, threading it to `RunContext`. Verify `tests/test_plan_import.py` passes unchanged.
- [x] 1.3 Add `tests/test_now_injection.py` pinning that a fixed `/fchg` fixture graded with an explicit `now` equals the same fixture graded with `now` omitted and the clock frozen to that value — so live behaviour is provably unchanged.
- [x] 1.4 Add a test in the same file asserting that one fixture graded at two different `now` values yields `PREDICTION_ONLY`/`NO_DATA` before the event and `RECORDED` after it, covering the spec's "judged against their recording instant" requirement at the unit level.

## 2. Archive acquisition

Downloads only — nothing in this group writes to the database.

- [x] 2.1 Add a `backfill = ["pyarrow>=18"]` optional extra to `pyproject.toml`, with a comment matching the existing lightgbm note on why it is not a base dependency. Verify `uv sync --extra backfill` succeeds and `uv sync` alone still installs no pyarrow.
- [x] 2.2 Add `data/archive/` to `.gitignore` and verify `git status` stays clean after a download.
- [x] 2.3 Create `src/dbrel/ingest/archive.py` with day-partition listing against the Hugging Face tree API and resolve-URL download into `data/archive/`, writing `manifest.json` with each file's upstream size and `oid`. Verify a single day downloads and a second invocation reports it reused rather than re-downloaded.
- [x] 2.4 Implement window resolution: default last 30 days, `--from`/`--to` overrides, extended one day either side for cross-midnight runs (design.md — Decision 5). Verify unit tests cover the default window, an explicit window, and the boundary extension.
- [x] 2.5 Add a `dbrel archive-fetch` command exposing window options and reporting bytes downloaded, files reused, and total on-disk size. Verify a one-day fetch reports non-zero downloaded then all-reused on repeat.
- [x] 2.6 Implement missing-hour detection over the acquired partitions and report hours absent from the archive. Verify against 2026-08-28, where hours 05–07 are known absent.

## 3. Replay

- [x] 3.1 Add row iteration to `archive.py`: read one partition's `timestamp`, `url`, `api_name`, `response_data`, `status_code` column-wise with pyarrow, skipping non-200 rows, and converting the naive Europe/Berlin `timestamp` to a UTC epoch via `zoneinfo` (design.md — Decision 7). Verify a test asserts the epoch conversion across a DST boundary.
- [x] 3.2 Implement EVA extraction from the `url` for both endpoints and verify a unit test covers a `/plan/EVA/date/hour` URL and a `/fchg/EVA` URL, including the leading-zero form (`08001506` → `8001506`).
- [x] 3.3 Implement sweep clustering: group `/fchg` rows into sweeps by timestamp gap threshold well under the ~6h cadence. Verify against a real partition that 2026-08-28 yields the observed sweeps near 00:54–00:58 and 15:20–15:25.
- [x] 3.4 Create `src/dbrel/ingest/replay.py` with `ArchiveReplaySource` exposing `plan(eva, date, hour)` and `changes(eva)` returning the existing `Response` dataclass, scoped to one sweep and answering only for tracked EVAs. Verify a test asserts a 404-equivalent for an untracked EVA and a 200 with correct bytes for a tracked one.
- [x] 3.5 Implement the day replay driver: `/plan` first via `import_plan(date=…, hours=…)`, then each `/fchg` sweep in chronological order via `run_tt_poll(now=<sweep instant>)` (design.md — Decision 3). Verify one archived day replays into a scratch database and writes stops.
- [x] 3.6 Add counters and a `--dry-run` mode reporting sweeps replayed, stops by quality grade, archived EVAs skipped as untracked, and tracked stations the archive never covered, without committing. Verify `--dry-run` leaves the database byte-identical.

## 4. Command surface

- [x] 4.1 Add a `dbrel backfill` command taking the window options, `--dry-run`, and a per-day resume point, reporting the full histogram from task 3.6. Verify `dbrel backfill --help` documents every option and a `--dry-run` single day runs end to end.
- [x] 4.2 Make the run resumable per day, recording completed days so an interrupted run continues rather than restarting. Verify interrupting after one day and re-running skips that day.
- [x] 4.3 Update `README.md` with a backfill section: what it is for, the `--extra backfill` install, the expected grade distribution (including why `NO_DATA` is a large share), and the explicit statement that `monthly_processed_data` is not used and why. Verify the section states the measured 26.6% never-reported figure and its operator-dependent range.

## 5. Correctness verification

This group is what distinguishes a working backfill from a plausible one — the spec's
central guarantees are all here.

- [x] 5.1 Add `tests/test_archive_replay.py` asserting the never-reported case: a stop present in a `/plan` fixture with no `ct` in any `/fchg` fixture is written with null actual times, null delays, and the absent-observation grade — never a zero delay.
- [x] 5.2 In the same file, assert the genuine-punctual case: a `/fchg` fixture publishing `ct == pt` yields a `RECORDED` stop with zero delay. Build the fixture from the verified Dormagen stop `-5342035254732082363-2608280320-4` (`ct="2608280359"`/`ct="2608280400"`).
- [x] 5.3 Assert the two cases are distinguishable after backfill — different `quality`, and a filter on `RECORDED` returns the punctual stop and excludes the never-reported one. Use the verified Lennestadt-Meggen stop `-9147199812937242171-2608280348-3` as the never-reported fixture.
- [x] 5.4 Assert forecast rejection and supersession: a `ct` from a pre-event sweep is not graded `RECORDED`, and a later post-event sweep for the same stop supersedes it and is.
- [x] 5.5 Assert idempotence and non-downgrade: replaying the same window twice leaves `trip_stop` unchanged, and replaying over a window already collected live neither duplicates rows nor replaces a `RECORDED` stop with a weaker grade.
- [x] 5.6 Assert archive gaps become measured gaps: a window with no covering response yields the uncovered-window grade with a `poll_gap_s` reflecting distance to the nearest covering response, not a zero delay.
- [x] 5.7 Assert station scoping: an archived EVA with no tracked station writes nothing and is counted as skipped; a tracked station absent from the archive is reported as uncovered and does not fail the run.

## 6. Live validation and sizing

- [x] 6.1 Replay one full archived day into a scratch database and record the grade histogram. Verify the never-reported share is near the measured ~26.6% expectation; investigate before proceeding if it is far off, since that would indicate a replay defect rather than an archive property.
- [ ] 6.2 Compare that day's backfilled rows against live-collected rows for the same Ruhr corridor date where overlap exists, reporting agreement on delay for stops both sources graded `RECORDED`. Verify disagreements are enumerated rather than averaged away.
- [ ] 6.3 Measure and report per-day database growth and replay wall-clock, and project both to 30 days. Verify the projection is recorded in the change before the full window is run, so the disk cost is known in advance.
- [ ] 6.4 Run the full 30-day window and report the final histogram, missing hours, and database size. Verify `dbrel stats` reflects the new national coverage and that a spot-checked station board renders backfilled history.
