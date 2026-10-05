# Tasks

Before every task, tell the user its duration estimate and the basis for it. The ranges below are
provisional. Every task ends with its own tests passing and its docs updated; "done" means both.

No training on the real data happens in this change. Runs against external services or the real
database are marked **(run)**, and each needs the user's go-ahead first. This change lands before
group 5 of `rebuild-arrival-model-for-full-year`, which then trains with the new default feature
set.

Test commands:
- uv suite:
  `uv run --offline pytest -q -p no:cacheprovider tests/ --deselect tests/test_ml_memcheck.py::test_the_check_writes_every_field`
- Torch tests: the ROCm venv command in the README's "Delay model" section.

## 1. Weather forecasts (server side)

- [ ] 1.1 **(run)** Probe Open-Meteo's Previous Runs API for three grid cells over the first
  backfilled month and the current month. Confirm that `*_previous_day1` exists for all four
  variables over the dataset range, and note the earliest date it covers. If coverage is short,
  stop and ask the user before continuing with group 1. Record the result in design.md's
  implementation notes. ~15 min.
- [ ] 1.2 `src/dbrel/weather.py`: the 0.5° grid, the station → cell map (every tracked station
  has a cell), and the `weather_forecast` model created by `create_all()`. Add
  `lookup(cells, hours, as_of)`, which returns the latest value issued at or before `as_of`, or
  NaN. Tests in `tests/test_weather.py`:
  - the grid covers every fixture station
  - the "Two forecasts for the same hour" scenario
  - NaN beyond the stored hours

  ~1.5 h.
- [ ] 1.3 The live refresh: `refresh_forecasts(session, client)` requests all cells with several
  coordinates per request, 8 days ahead, and stores rows with `issued_at` = fetch time. It runs
  as a `collect` step every 6 hours, and a failure logs the last successful refresh and
  continues. Add thinning: rows older than 3 days keep only the latest issue at least 24 h
  before each hour. Add a `dbrel weather-refresh` command. Tests use a fake HTTP client:
  - requests are the same for 1 and 10,000 callers, because the request count depends only on
    the cells
  - usage units per day are ≤ 10% of 10,000
  - a failing client leaves the stored rows usable
  - the thinning scenario

  README: add a "Weather forecasts" subsection. ~2 h.
- [ ] 1.4 The archive: `dbrel weather-archive --from --to` downloads `*_previous_day1` in 14-day
  chunks per batch of cells. It stores `issued_at = valid_hour − 24 h`, skips stored chunks,
  paces itself under the hourly and minute limits, and reports hours it couldn't get. Tests with
  a fake client: resume after an interruption requests only the missing chunks; pacing never
  exceeds the configured per-minute units. README: the command and its expected duration.
  ~2 h.
- [ ] 1.5 **(run)** Run the archive for the dataset range, and one live refresh. Report stored
  rows, units used and hours missing. The download is unattended; hands-on work is about 10 min.

## 2. Notices

- [ ] 2.1 `tt_message.first_seen_at`: `schema_migrate.py` adds it idempotently, and the poller
  sets it on insert. Tests: migrating twice is a no-op; a polled fixture message has
  `first_seen_at`. ~45 min.
- [ ] 2.2 `src/dbrel/ml/notices.py`: per-trip publication and withdrawal times via SQL (min `ts`
  per notice, first deleted version, `msg_type='h'`, category prefixes). Add
  `notice_features(times, cutoff, stop_sequence)` for the three features. Tests cover the spec
  scenarios:
  - announced days ahead
  - published after the cutoff
  - withdrawn
  - information notices are ignored
  - the stop flag only at the notice's stop

  ~1.5 h.
- [ ] 2.3 A `dbrel ml notice-check` command. It reports notices per trip by month, which flags
  backfilled months far below the live rate. It also reports, once `first_seen_at` data exists,
  the share of notices first seen after a call's cutoff. Test: fixture counts. README: what the
  check means and the 5% threshold from design.md. ~1 h.
- [ ] 2.4 **(run)** Run `notice-check` on the real database: once now for coverage, and again
  after two weeks of `first_seen_at` for the lag. If the lag exceeds 5%, apply design.md's
  fallback and record it. ~10 min each.

## 3. Recent delays

- [ ] 3.1 The `recent_delay_daily` model and `derive/recent_delays.py`, which fills it
  incrementally for service dates with newly finalized trips. It applies the same target filter
  as the dataset query, at train and line level, and stores max `finalized_at`. It runs as a
  `collect` step after finalize, and as `dbrel build-recent-delays`. Tests:
  - rows match a hand count on the fixture
  - re-running adds nothing
  - a newly finalized trip updates only its dates

  ~1.5 h.
- [ ] 3.2 `recent_features(...)`: the D−7…D−1 window, where days finalized after the cutoff are
  excluded, the mean is NaN at n = 0, and there is a vectorised form for shards. Tests cover
  the spec scenarios:
  - 8, 9, 7, 10, 8 gives 8.4 over 5 runs
  - a run finalized after the cutoff doesn't count
  - no train runs but line runs

  ~1.5 h.
- [ ] 3.3 **(run)** Build `recent_delay_daily` on the real database; report rows and time.
  ~15 min.

## 4. Features, dataset and serving

- [ ] 4.1 `features.KNOWN_AHEAD` and log1p scaling for the counts. Add `missing_share` masking
  (notices 10%, weather 5%, seeded) recorded in the prep and manifest. Tests:
  - masked rows are NaN and get the `_missing` indicator
  - the recorded shares match
  - "Missing is not zero": 0 and NaN encode differently

  ~1 h.
- [ ] 4.2 `full_history`: store per-trip notice times and the build-time recent and weather
  columns in the shards, bump `CODE_VERSION`, and compute the known-ahead features on read.
  `full_year` statistics and prepared sets include them. Tests:
  - a fixture shard with notices, runs and forecasts gives the hand-computed features
  - an unchanged shard is reused only while the code version matches

  README: shard contents. ~2.5 h.
- [ ] 4.3 `web/queries._model_input` computes the new features only when the artifact's
  `features_numeric` lists them, using the same functions with cutoff = now, cached per trip
  and hour. Tests:
  - "Training and prediction build features the same way": the same fixture call through both
    paths gives identical values
  - "An older artifact keeps its inputs": the TabM `20260924T183944Z` input is unchanged
  - "Everything unknown" still predicts

  Measure the board endpoint's time before and after on the fixture and report both. ~2 h.

## 5. Reports and the ablation

- [ ] 5.1 `metrics.score_arrivals` gains `over_3_min` and `over_5_min`. `evaluation_report`
  adds a `has_works_notice` breakdown, and rankings and `final.json` show both shares. Tests:
  shares equal 1 − within on a fixture, and the breakdown sums to the total. ~1 h.
- [ ] 5.2 Full-year search: the default numeric set becomes `full`, and a `no-known-ahead`
  variant joins `no-doy` on the compact track. The report states the MAE and over-5-minute
  differences. Update `tests/test_ml_full_year_search.py`: the ranking holds the variant, and
  the report shows both differences. The rebuild change's design notes gain one line about the
  new default. ~1 h.

## 6. The app

- [ ] 6.1 `ModelDelta.tsx` shows the point figure in whole minutes and the p25–p75 range next
  to it, using one rounding helper from design.md D6 (halves away from zero, never `-0`). The
  tooltip keeps the unrounded values. Update the component's header comment, which still
  describes the one-decimal cue. The helper lives in a new import-free `src/lib/minutes.ts`
  (`delay.ts` imports through the `@/` alias, which plain Node can't resolve), so it can be
  tested without adding a test framework. Add `src/lib/minutes.test.ts`, run with
  `node --test` (Node 24 runs TypeScript directly), covering 2.6 → +3, 2.5 → +3, −0.4 → +0
  and −1.5 → −2. Check with `tsc --noEmit -p tsconfig.json` and `npx vite build`, and add a
  screenshot of a board with rounded figures and ranges to the task notes. ~1 h.
- [ ] 6.2 `ShellV2.tsx` footer credits Open-Meteo (CC BY 4.0) with a link. Same checks and
  screenshot. ~15 min.

## 7. Wrap-up

- [ ] 7.1 Run the full uv suite and the torch tests; all pass. ruff shows no new issues in
  touched files. Update the live-accuracy skill doc if its labels or expected test count
  change. ~20 min.
- [ ] 7.2 Tick the tasks, and add implementation notes to design.md where the code differs from
  the plan. ~15 min.
