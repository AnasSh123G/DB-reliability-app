# Tasks

Role tags mark which of the three apply-time roles (design.md > Decisions > Execution split)
each task belongs to: **[fetch/haiku]** for backfill-fetcher and holiday-fetcher, **[db/sonnet]**
for database-organizer. Tasks with no tag are ordinary sequenced work with no cost-sensitivity
reason to delegate.

**Operational note (Anas, 2026-09-23):** whenever a subagent/background task is running as part
of applying these tasks and its output is still pending, state an estimate of how long it
should take, not just that it's running.

**Operational note (Anas, 2026-09-23):** for every task worked on here, first check whether the
current model is more than the task needs, or whether it's worth spinning up a subagent to do
it instead — don't default to doing everything inline on whatever model is currently running.

## 1. Backfill data readiness

- [x] 1.1 **[fetch/haiku]** Run `dbrel backfill --days 30` (or resume it) against the real
      database and verify it completes with exit code 0 and a non-empty `by_quality` histogram
      containing `RECORDED` rows in the CLI output.
- [x] 1.2 **[fetch/haiku]** Verify `trip_stop` has `RECORDED`-quality rows for more than one
      distinct `station_id` and more than one distinct `line_id` (via a quick count query),
      confirming national coverage rather than a single-station smoke test.

## 2. Holiday data fetch

- [x] 2.1 **[fetch/haiku]** Fetch public holiday dates for all 16 German Bundesländer, for every
      calendar year the tracked data spans (from the earliest to latest `trip.service_date`),
      from the chosen external source (design.md > Decisions > Holiday source). Verify the
      output has entries for all 16 states and each state exposes at least its national-holiday
      dates.
- [x] 2.2 **[fetch/haiku]** Write the fetched holidays to an intermediate file (state -> list of
      `{date, name}`) and verify it parses back as valid JSON with 16 top-level state keys.

## 3. Schema changes

- [x] 3.1 **[db/sonnet]** Add `Station.state` (nullable string) to `models.py` and verify the
      column exists after `create_all()` on a fresh database (inspect via
      `sqlite3 ... ".schema station"` or equivalent).
      Verified: fresh in-memory `create_all()` shows `state` in `PRAGMA table_info(station)`;
      real database migrated via `dbrel migrate-holiday-schema` (idempotent `ALTER TABLE`, since
      `create_all()` never patches an existing table).
- [x] 3.2 **[db/sonnet]** Change `PublicHoliday` primary key from `date` alone to composite
      `(date, state)` in `models.py` and verify the new constraint via `create_all()` on a fresh
      database, confirming two rows with the same date but different states can coexist.
      Verified on a fresh database and confirmed on the real one (529 rows, 16 distinct states,
      `PRIMARY KEY (date, state)` per `.schema public_holiday`). Also added `partial` (0/1),
      not in the original plan -- see holiday-calendar spec's new "partial holiday" requirement.
- [x] 3.3 **[db/sonnet]** Rebuild the local database schema (`create_all()`) and verify existing
      `trip`/`trip_stop`/`station` row counts are unaffected (only `public_holiday` is emptied
      by the key change, per design.md > Migration Plan).
      Verified against the real database: station 5,422, trip 1,256,966, trip_stop 14,936,906,
      all unchanged before/after `dbrel migrate-holiday-schema`. `public_holiday` was empty
      before migration (0 rows), so nothing was actually lost by the drop.

## 4. Station state resolution

- [x] 4.1 **[db/sonnet]** Add a bundled German 16-state boundary dataset and point-in-polygon
      lookup (design.md > Decisions > Station state resolution) and verify it correctly
      classifies a handful of known reference stations (e.g. a station clearly in Bavaria, one
      clearly in Hamburg) against their real Bundesland.
      Verified (`tests/test_geo_states.py`, plus a manual check): München Hbf -> DE-BY, Hamburg
      Hbf -> DE-HH, Köln Hbf -> DE-NW, Dresden Hbf -> DE-SN, Berlin Hbf -> DE-BE, a Paris
      coordinate -> None. Hand-rolled ray-casting (`geo_states.py`), no new dependency -- see
      design.md's updated decision.
- [x] 4.2 **[db/sonnet]** Resolve and persist `Station.state` for every tracked station and
      verify the count of stations with a resolved state, plus report (not silently drop) the
      count left `NULL` per design.md's exclusion rule.
      Verified against the real database (`dbrel resolve-station-states`): 5,389 of 5,422
      resolved across all 16 states, 33 NULL (border/lakeside stations -- Konstanz,
      Laufenburg(Baden), Kleinblittersdorf, Lindau-Insel, ...), printed and reported, not
      dropped.

## 5. Holiday calendar capability

- [x] 5.1 **[db/sonnet]** Rewrite `holidays_de.py` (or its replacement) to load per-state holiday
      dates from task 2.2's intermediate file into `public_holiday` with the new composite key,
      and verify `ensure_holidays`-equivalent logic populates all 16 states' rows.
      Verified against the real database (`dbrel load-holidays`): 529 rows added, 16 distinct
      states, 3 marked `partial` (Augsburg's Friedensfest, 2025-2027).
- [x] 5.2 **[db/sonnet]** Implement per-station holiday-flag resolution: a date is a holiday
      timetable day for a station if it's a Sunday OR a public holiday in that station's own
      state. Verify with the spec scenario: same date, two states, one observing a holiday and
      one not, resolves to different flags for stations in each.
      Verified (`tests/test_label_holidays.py`): implemented as `trip_stop.is_holiday`, filled by
      a single set-based SQL UPDATE (`derive/label_holidays.py`), not a per-query function --
      the spec's "querying two stations" scenario is exercised as "labelling two stations".
      Extended with a NULL case the original plan didn't cover: an unresolved-state station on a
      state-specific (not all-16) holiday date gets `is_holiday = NULL`, not a guess -- see
      holiday-calendar spec's new requirement.
- [x] 5.3 Write/adapt unit tests covering: a state-specific holiday (e.g. Fronleichnam) applying
      only in its own state; a nationwide holiday applying everywhere; every Sunday flagging as
      holiday regardless of state; a station with unresolved state being excluded rather than
      defaulted.
      `tests/test_holidays_de.py` (`TestIsHoliday`, `TestHolidayTable`) and
      `tests/test_label_holidays.py` cover all four cases plus the partial-holiday and NULL-label
      cases added along the way.

## 6. Stop-delay averages

- [x] 6.1 **[db/sonnet]** Implement derivation of the averaging key
      `(station_id, line_id, direction, scheduled_time_of_day, holiday_flag)` for a `trip_stop`
      row, reusing `web/queries.py::_direction_of` for direction (design.md > Decisions >
      Direction) and Berlin-local `HH:MM:SS` of the station's own `supposed_departure_utc` for
      scheduled time-of-day. Verify with a unit test: two trips of the same line/direction at
      two different daily start times produce two different keys at a shared downstream station.
      Verified (`tests/test_stop_averages.py`). Direction logic moved to `dbrel/direction.py`,
      shared by `web/queries.py` (thin aliases kept for existing call sites) and
      `derive/stop_averages.py`, so there is exactly one implementation. `sched_time` is 'HH:MM'
      (not 'HH:MM:SS' -- GTFS/IRIS scheduled times don't carry seconds precision worth keying
      on), of `COALESCE(supposed_departure_utc, supposed_arrival_utc)` (arrival fallback for
      terminating stops), computed via a Python-derived DST-transition CASE
      (`derive/berlin_time_sql.py`), not SQLite 'localtime'.
- [x] 6.2 **[db/sonnet]** Add the average table to `models.py` (mean `arr_delay_s`, mean
      `dep_delay_s`, sample count, plus the key columns) and verify it's created via
      `create_all()`.
      `StopDelayAverage` / `stop_delay_average`, verified via fresh `create_all()` and present on
      the real database after `dbrel migrate-holiday-schema`.
- [x] 6.3 **[db/sonnet]** Implement the aggregation build (in `src/dbrel/derive/`) over
      `RECORDED`-quality, non-cancelled `trip_stop` rows only, grouped by the task 6.1 key, and
      verify with a unit test: a cancelled stop and a non-`RECORDED` stop are excluded from both
      the mean and the sample count.
      `derive/stop_averages.py`. Verified (`tests/test_stop_averages.py`), extended with one more
      exclusion the plan didn't name: rows with `is_holiday IS NULL` (undecidable) are excluded
      from both averages too -- see stop-delay-averages spec's new scenario.
- [x] 6.4 **[db/sonnet]** Add a CLI command to (re)build the average table on demand and verify
      running it twice in a row with unchanged `trip_stop` data produces byte-identical average
      values (per stop-delay-averages spec's "precomputed, not live" scenario).
      `dbrel build-stop-averages` (full DELETE + INSERT per run). Verified deterministic in
      `tests/test_stop_averages.py::test_build_is_deterministic`. On the real database: 985,079
      rows in 30.2s.

## 7. Integration verification

- [x] 7.1 Run the full pipeline end to end (backfill already populated -> holiday load ->
      station state resolution -> average build) against the real database and verify the
      user's example query works: retrieve the average arrival/departure delay for a specific
      line, station, direction, and scheduled time, split by holiday flag, with a non-zero
      sample count on at least one row.
      Ran in order: `migrate-holiday-schema` -> `resolve-station-states` -> `load-holidays` ->
      `label-holidays` -> `build-stop-averages`. Example: `dbrel stop-average --line RE7
      --station "Neumünster" --direction 0 --time 07:31` returns WEEKDAY (n_arr=49, mean
      +171s; n_dep=47, mean +200s) and HOLIDAY (n_arr=12, mean +50s; n_dep=12, mean +50s) rows.
- [x] 7.2 Verify the full existing test suite still passes (`pytest`) after the schema changes,
      confirming no unrelated regression from the `public_holiday` key change or the new
      `Station.state` column.
      `282 passed` (`.venv/bin/python -m pytest -q`), up from ~260 baseline with the new test
      files for geo_states, station_states, label_holidays, and stop_averages.

## 8. Follow-up fixes (post-7.2 review found two defects breaking rules 2 and 3)

- [x] 8.1 **[db/sonnet]** Direction was computed against a single nationwide "longest route"
      axis per `service_line` id, but a line id is not one physical line (short names like
      "RE7"/"S1" are reused by unrelated regions) -- line 97 "RE7" split 416/62 instead of
      splitting per corridor. Fixed by partitioning a line's trains into connected components
      (`dbrel/direction.py::_connected_components`, union-find, >=2 shared station names) before
      computing a canonical axis and direction per component. Verified: line 97 now splits into
      9 components (sizes 124/124/79/44/38/34/17/17/1), each roughly balanced (e.g. the two
      124-train components split 62/62 and 64/60); the Y-branch component (Hamburg<->Kiel/
      Flensburg) stays one component with both away-from-Hamburg branches sharing one direction
      value and both toward-Hamburg branches sharing the other. 226 of 834 lines with trains
      have >1 component. `tests/test_direction_components.py`; existing RB33 short-working
      behaviour unchanged (`tests/test_line_directions.py` still passes unmodified).
- [x] 8.2 **[db/sonnet]** `trip_start_time` was informational-only and not part of the averaging
      key, so two different trips of the same line/direction reaching a shared station at the
      same scheduled clock time (RE7's coupled portions: 11159 Flensburg->Hamburg-Altona and
      11259 Kiel->Hamburg-Altona, both timed to call at Neumünster 07:31) were pooled into one
      row. Fixed: `trip_start_time` joined the primary key
      (`station_id, line_id, direction, trip_start_time, sched_time, is_holiday`), derived PER
      TRIP (not per train) from `trip.source_run_key`'s embedded `yyMMddHHmm` IRIS stamp for
      `source='tts'` trips (Berlin wall clock already, no DST conversion needed), falling back to
      `train.first_departure_sec` for `source='gtfs'` trips (none currently reach `RECORDED`
      quality). Trips where neither yields a value are excluded and counted
      (`excluded_no_start_time` in the build report) -- 0 on the real database, verified against
      the full 11,866,945-row RECORDED/non-cancelled/labelled set. `stop_delay_average` was
      dropped and recreated (schema_migrate.py) since its key changed; it's a table this change
      created and fully rebuilds every run, so nothing was lost.
      `tests/test_trip_start_time.py` (negative `daily_trip_id` doesn't shift the stamp;
      malformed run key excluded and reported), `tests/test_stop_averages.py`'s new
      coupled-portions test. Real database: rebuilt to 1,315,685 rows (511,774 with n_dep>=5) in
      69.1s. Re-ran `dbrel stop-average --line RE7 --station "Neumünster" --direction 1 --time
      07:31`: two trips now separate, start=06:17 (Flensburg->Hamburg-Altona, WEEKDAY n_arr=23,
      n_dep=22) and start=07:05 (Kiel Hbf->Hamburg-Altona, WEEKDAY n_arr=22, n_dep=21), plus
      their small holiday-flagged counterparts (n=4 each) -- the previously-misrouted opposite-
      direction run (Hamburg-Altona->Kiel Hbf) now correctly appears only under `--direction 0`.
      CLI gained `--start HH:MM` and shows `(origin -> destination)` per row.
      Full suite: `289 passed`.
