# Tasks

## 1. Sample stations and fetch live data

- [x] 1.1 Add a station sampler: a random, without-replacement draw of N stations (default
      3,000) from the station table. Verify: a unit test shows no duplicates and honours a
      given size and random seed.
- [x] 1.2 Add a per-station live fetch using the existing `TimetablesClient.changes(eva)` (the
      same `/fchg` call `dbrel collect` makes) and `parse_timetable` to read it. A failed or
      empty fetch is caught and the station is skipped, not fatal. Verify: a test with a mocked
      client shows a 4xx/5xx or empty response is skipped and counted, and the run continues.
- [x] 1.3 From each station's parsed stops, keep only sides (`arr`/`dep`) with a real, published
      outcome: a changed time present (not merely a planned time — that's `_has_happened`'s
      fallback for a different purpose in `changes_poller.py`, and yields no delay to compute),
      more than `GRACE_S` (`changes_poller.GRACE_S`) in the past, and not cancelled. Verify: a
      test with a stop that only has a planned time, one whose changed time is a near-future
      estimate, one that's cancelled, one with only its arrival realized, and one fully
      realized, each keep exactly the sides the spec says they should.

## 2. Build model features for each realized stop

- [x] 2.1 Resolve each realized stop's train to a local `Train` row via `train_identity_key`
      (from its category and number), and its route shape via `dbrel.ml.timetable.calls_at`
      (`station_stop`'s static timetable pattern — not `trip_stop`), returning the matching
      `ScheduledCall` or `None` if the train has no local match. Verify: a test against a
      fixture database returns the expected `stop_rank`/`n_stops` for a known train+station and
      `None` for one never imported.
- [x] 2.2 Build each realized stop's feature row with `ScheduledCall.raw()` and
      `direction_for()` (`dbrel.ml.features`/`dbrel.ml.timetable`) — the exact function
      `predict.py`'s live prediction path already uses — filling in the schedule fields from
      task 2.1 and the holiday flag/weekday from the stop's own service date. A stop with no
      local train match is skipped and counted, never given guessed values. Verify: for a
      handful of stops matched against calls already in the local database, this produces the
      same feature values `predict.py`'s own encoding would for the same call.

## 3. Get both predictions

- [x] 3.1 Compute TabM's prediction for each assembled stop via `DelayModel.current`, the same
      class `predict-delay` and the browser endpoint use. Verify: for a stop also reachable
      through `predict-delay`, both give the same p25/p50/p75.
- [x] 3.2 Compute the stored average for each stop by calling `_delay_pred` directly (or the
      shared helper it wraps), not a recomputed baseline. Verify: for a stop the station board
      API already shows an average for, this gives the identical figure.

## 4. Score and report

- [x] 4.1 Score both predictors against each realized side's real recorded delay with the
      existing `dbrel.ml.metrics` (MAE, pinball loss, band coverage), broken down by day and by
      segment/holiday flag, the same shape as the bake-off report. A side with no stored average
      counts as "not covered" for the average only, while TabM is still scored. Verify: a test
      with a known set of realized stops and predictions produces the expected per-predictor,
      per-breakdown numbers by hand.
- [x] 4.2 Write a report (numbers table + short write-up) to `data/ml/studies/<timestamp>/`,
      including how many stations were sampled, how many yielded no realized stops, how many
      stops were skipped for no route-shape match, and the total comparisons scored per side.
      Verify: running against a fixture produces a report file with every one of these figures
      present.

## 5. Command and launch

- [x] 5.1 Add `dbrel study live-accuracy [--sample-size N] [--seed S]`, wiring tasks 1-4
      together. Verify: `--help` documents both flags, and a run against a fixture database
      (fake `TimetablesClient`) completes and writes a report.
- [x] 5.2 Confirm the command is read-only: running it against a fixture database leaves
      `trip_stop`, `stop_delay_average` and `train_stop_delay_average` byte-identical before and
      after, and no file under `data/models/` is touched. Verify: a test asserts this directly.
- [x] 5.3 Document, in the command's own help text or a short README note, that a real run
      makes up to `--sample-size` live requests and should go through the project's capped
      launcher (`dbrel ml launch` or equivalent) rather than running inline in a terminal
      session, per standing practice for anything that runs unattended for several minutes.

## 6. Verification on the real data

- [x] 6.1 Full test suite passes.
- [x] 6.2 Fix the fetch after the first real run: join `/plan` (last 2 hours) with `/fchg` by
      stop id, since `/fchg` alone has no train number or planned time. Verify: a test where
      `/plan` holds the train and `/fchg` only the changed time yields one realized stop with
      the right delay; live, Bochum/Essen give realistic 0-3 minute delays.
- [x] 6.3 Replace the report's statistics with plain ones: average minutes off, share within
      1/3/5 minutes, and how often each predictor was closer, on the same stops. Verify: tests.
- [x] 6.4 **Only on Anas's go**: run `dbrel study live-accuracy` for real, against the live DB
      Timetables API, and present the resulting report — total stations sampled, comparisons
      scored per side, and TabM vs. the stored average's MAE/pinball/band coverage against the
      real outcomes.
