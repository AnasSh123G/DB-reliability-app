# Design

## Context

See proposal.md for why. The current state that shapes the approach:

- **Model inputs are built on the server.** The browser only runs the ONNX model.
  `web/queries._model_input` builds each call's input from `features.compute_features` and the
  artifact's prep. Training builds the same features in `full_history` (monthly shards, raw
  columns, features computed on read) and `full_year` (streamed statistics, prepared sets).
- **Artifacts declare their inputs.** A prep whose numeric list differs from `NUMERIC` records it
  as `features_numeric`. Missing numerics already work: NaN becomes 0 plus a `<name>_missing`
  indicator column whenever training saw a NaN (`arrival.prep_layout`, `arrival_inputs`).
- **Notices are already collected.** `tt_message` holds IRIS messages from the `/fchg` polls,
  including replayed archive days: `msg_type`, `category`, `ts`, `valid_from/to`, `deleted`,
  `trip_id`, `stop_sequence`. Construction notices have categories starting with "Bauarbeiten";
  disruption notices start with "Störung" or "Großstörung". No column records when the collector
  first stored a message.
- **Finalization.** Runs are finalized into `trip`/`trip_stop` with `finalized_at`. The training
  cutoff rule already exists: history must be finalized before the call's cutoff, which is the
  arrival minus 5 hours.
- **Stations and grid.** All 5,422 stations are tracked and have coordinates. A 0.5° grid needs
  208 cells to cover them; a 0.25° grid needs 703.
- **The app.** `ModelDelta.tsx` shows the point figure and puts p25–p75 only in the `title`
  tooltip.

## Goals / Non-Goals

**Goals:**
- Every new input is computed identically in training and at serving, from data known at the
  cutoff, and the same code path serves both where possible.
- Serving adds no per-user external requests, and at most a few database lookups per call.
- A model without the new inputs, and the deployed artifacts, keep working unchanged.

**Non-Goals:**
- Live delays of running trains (postponed).
- Delay causes other than the listed notice kinds; free-text notice contents.
- Hyper-local weather, or weather observations as inputs.
- Changing the size budgets, tracks or split rules of the full-year change.

## Decisions

### D1. Notice inputs from `tt_message`, counted as of the cutoff

Features:
- `works_trip_n`: distinct construction notices on the trip
- `disruption_trip_n`: distinct disruption notices on the trip
- `works_at_stop`: 0/1, a construction notice at this stop's `stop_sequence`

A notice counts as published at the minimum `ts` of its versions. It is withdrawn once a version
with `deleted = 1` exists at or before the cutoff. Only `msg_type = 'h'` counts, with the
category prefixes above.

- **Training.** The full-history builder aggregates per trip once per month with SQL (min `ts`
  per notice, withdrawal time) and stores per-trip arrays of publication and withdrawal times.
  Counts are computed on read against each row's cutoff, like the other on-read features.
- **Serving.** `_model_input` runs the same aggregation for the one trip with cutoff = now. A
  call whose trip has no `trip` row yet (plan not imported) gets NaN, which means missing.
- **Teaching "missing".** Historical rows always have a trip. A seeded, recorded share (default
  10%) of training rows get their notice features set to NaN, so the model learns the missing
  case (spec: "Inputs unknown at prediction time are marked missing").
- **First-seen time.** `tt_message` gains a nullable `first_seen_at` column, set by the poller
  on insert from now on. The notice rule stays on `ts` because the history has no first-seen
  time. Once two weeks of `first_seen_at` exist, a check measures how often a notice was stored
  later than its call's cutoff. See Risks.

Alternatives considered:
- A single "any notice" flag: loses the distinction between construction (planned, strongly
  predictive) and disruption (mostly reactive).
- Parsing message text: not stored, and brittle.

### D2. Recent delays from a daily aggregate table

A new table `recent_delay_daily` has key (level, key_id, station_id, service_date), where level
is `train` or `line`, and holds n, sum of arrival delay in seconds, and max `finalized_at`. Only
arrivals that qualify as training targets count, under the same filter as the dataset query.

- It is filled by SQL from `trip_stop`, incrementally for dates whose trips were finalized since
  the last run. A `collect` step runs it after `finalize`, and `dbrel ml build-full-dataset`
  runs it before building.
- Features: `recent_train_mean_min`, `recent_train_n`, `recent_line_mean_min`,
  `recent_line_n`. The window is service dates D−7 … D−1. A day counts only if its
  `max finalized_at` is at or before the row's cutoff. The mean is NaN when n = 0.
- **Training.** The shard builder loads the aggregate rows for the month and the 7 days before,
  sorted by key, and computes the window per row with vectorised lookups (7 per level).
- **Serving.** `_model_input` reads the 7 rows per level for the call through a covering index,
  and caches them per (train, station, date) for 15 minutes.

Alternatives considered:
- Reusing the stored averages (`train_stop_average`): they cover all history, not the recent
  week, and aren't cut at the forecast time.
- An exponentially weighted history: more tuning for the same idea. Keep 7 days, and revisit
  only if the ablation shows value.

### D3. Weather: Open-Meteo, 0.5° grid, stored with issue times

- **Grid.** Cells are floor(lat/0.5) × floor(lon/0.5), and each cell is queried at its centre.
  208 cells cover every station, and a station → cell map is computed once. At 0.5°, a cell is
  about 55 × 35 km, enough for fronts, storms and snow.
- **Variables.** Hourly `temperature_2m`, `precipitation`, `snowfall`, `wind_gusts_10m`.
  Features: `wx_temp_c`, `wx_precip_mm`, `wx_snow_cm`, `wx_gust_kmh`, at the call's scheduled
  hour (UTC, rounded down).
- **Live.** A `collect` step refreshes every 6 hours, requesting 8 days ahead for all 208 cells
  with multiple coordinates per request. That is about 830 units a day, 8% of the free
  allowance. `issued_at` is the fetch time, which is when the value became known to us.
- **Storage.** A new table `weather_forecast` has key (cell, valid_hour, issued_at) and the four
  values. Rows younger than 3 days are kept in full. Older rows are thinned to the latest issue
  at least 24 h before each valid hour, which is exactly the training definition. That is about
  1.8 M rows a year.
- **Archive for training.** Open-Meteo's Previous Runs API gives each variable as forecast about
  1 day earlier (`*_previous_day1`). `dbrel weather-archive --from --to` downloads it for all
  cells in 14-day chunks, resumably. The chunks stay under the free limits: about 5,400 units for
  11 months, spread over two days. Rows are stored with `issued_at = valid_hour − 24 h`.
  Task 1.1 first verifies that the archive covers the dataset's date range. If it doesn't, the
  work stops and the user decides; the fallback isn't assumed.
- **Lookup.** `weather.lookup(cells, hours, as_of)` returns the latest value with
  `issued_at ≤ as_of`, or NaN. Training uses as_of = the row's cutoff, which with archive rows
  always resolves to the day-ahead forecast. Serving uses as_of = now.
- **Teaching "missing".** A recorded 5% of training rows get NaN weather, for dates beyond the
  forecast.
- **Credit.** Open-Meteo's data is CC BY 4.0. The frontend has no about page, so the v2 shell
  (`ShellV2.tsx`) gets a one-line footer, "Weather data by Open-Meteo.com (CC BY 4.0)", linking
  to open-meteo.com. It shows on every v2 view.

Alternatives considered:
- DWD open data: free for commercial use, but has no easy archive of past forecasts, so training
  would learn from observations and meet forecasts at serving.
- Per federal state (16 points): too coarse for storms and snow.
- 0.25° grid: 703 cells, about 3.4× the usage, for little gain at this model's resolution.

### D4. Features, prep and artifact compatibility

- `features.py` gains `KNOWN_AHEAD = (works_trip_n, disruption_trip_n, works_at_stop,
  recent_train_mean_min, recent_train_n, recent_line_mean_min, recent_line_n, wx_temp_c,
  wx_precip_mm, wx_snow_cm, wx_gust_kmh)`.
- The full-year search's numeric sets become:
  - `full`: `ARRIVAL_NUMERIC + KNOWN_AHEAD`, the default for all trials
  - `no-known-ahead`: `ARRIVAL_NUMERIC`, the new ablation
  - `no-doy`: the existing ablation, which keeps the new inputs
- Counts are log1p-scaled before standardising. Weather values are standardised; robust scaling
  already bounds outliers.
- Raw sources are stored in the shards, so a feature-definition change needs no rebuild:
  - per-trip notice times
  - the daily aggregate lookups, done at build time, because they are joins
  - the weather lookups, done at build time from the local tables
- **Serving.** `_model_input` computes the new features only when the artifact's
  `features_numeric` asks for them. Older artifacts never trigger the extra lookups.

### D5. Reports and the ablation

- `metrics.score_arrivals` gains `over_3_min` and `over_5_min` shares, which are
  1 − within, over all eligible arrivals.
- `evaluation_report` adds a `has_works_notice` group. The ranking and `final.json` show
  over_3/over_5 next to MAE.
- The compact track's variant list becomes two variants after the fixed trials: `no-doy` and
  `no-known-ahead`.

### D6. Whole minutes and the range in the app

`ModelDelta.tsx` renders the point figure in whole minutes, then a smaller muted range, for
example `ML +1 min (+0 … +4)`. A shared helper rounds to the nearest minute with halves away
from zero; JavaScript's `Math.round` rounds −1.5 to −1, so it isn't used directly. A result of
zero shows as `+0`, never `-0`, matching the stored average's `+` prefix. The tooltip keeps the
rule description and the unrounded point and range with one decimal. No data change:
`SidePrediction` already carries `p25_min`/`p75_min`.

This replaces the one-decimal display chosen in `add-browser-delay-predictions` D4, which used
the number format to mark the model figure apart from the average. The `Ø` and `ML` labels
already do that, and one decimal suggested a precision the model doesn't have.

## Risks / Trade-offs

- **[Risk] A notice's `ts` is earlier than when the collector could have seen it.** Training
  would count a notice that serving wouldn't have yet, and the model would over-trust notices.
  → Record `first_seen_at` from now on, and measure the lag after two weeks (task 2.4). If more
  than 5% of notices are first seen after their call's cutoff, switch the live-data rule to
  `first_seen_at`, and state the limitation for the backfilled months in the report.
- **[Risk] Backfilled days have fewer notices than live days.** The archive's sweep timing
  differs from the live polls. → Compare notices per trip by month once the backfill is in
  (task 2.4). A month far below the live rate is flagged in the build manifest and the report.
- **[Risk] Serving forecasts are fresher than training forecasts** (latest versus day-ahead).
  → This is conservative: the model learns from less accurate forecasts than it gets. The live
  accuracy study will show whether weather-heavy days behave differently.
- **[Risk] The Previous Runs archive doesn't cover the whole backfill range.** → Task 1.1 checks
  this first, and the user decides before any fallback.
- **[Risk] A recent-delay input over-reacts to one bad day.** The mean over 1–2 runs is noisy.
  → The run count is an input, so the model can learn to discount small n.
- **[Trade-off] More server work per call.** Three small indexed queries per call are cached
  per trip and hour. The board endpoint's timing is measured before and after (task 4.3).
- **[Trade-off] These inputs won't catch sudden disruptions.** That is expected; only live data
  would.

## Migration Plan

1. `create_all()` creates `recent_delay_daily` and `weather_forecast`. `schema_migrate.py` adds
   `tt_message.first_seen_at` with the same idempotent `ADD COLUMN` check it already uses. It is
   additive; nothing existing changes.
2. Deploy the collector steps (weather refresh, daily aggregates). They only write the new
   tables.
3. Run `dbrel weather-archive` over the dataset range, then `build-full-dataset --full` (feature
   code version bump), then the full-year change's group 5 with the new default feature set.
4. Serving picks up the new inputs only through a new artifact that asks for them. Rollback is
   `promote` back to `20260924T183944Z`, as before. The new tables can stay.

## Open Questions

- The exact missing shares (10% notices, 5% weather) could later be matched to the measured
  share of far-ahead predictions in the web logs. The code records them, so changing them needs
  no redesign.
