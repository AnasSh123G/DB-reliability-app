# Proposal

## Why

TabM and the stored averages have only ever been scored offline, against the same collection
run their training/averages data came from. Anas wants an independent check: fetch real,
freshly-happened arrivals and departures directly from the DB Timetables API for a sample of
stations, score TabM and the stored averages against them, and see how each actually holds up
against what really happened — data untouched by the training pipeline, collection schedule,
or the earlier bake-off's date split.

Concretely: if someone had looked up a 13:00 train at 8:00 this morning, they'd have seen
TabM's prediction and the stored average for that stop. Now that it's past 13:00, this asks how
far off both were from what actually happened — at the scale of 3,000 stations rather than one
lookup. A prediction depends only on the trip's schedule, never on anything that happens
between when it's made and when the trip runs, so scoring it after the fact (from the same
fetch that shows the trip already happened) gives the identical number an 8:00 lookup would
have — one live call per station, covering every train that station saw, is enough.

## What Changes

- A new one-shot CLI command, `dbrel study live-accuracy`, that:
  - Samples 3,000 stations at random from the station table.
  - For each station, makes a single live call to the DB Timetables API (`/fchg/<eva>`, the
    same endpoint `dbrel collect` already polls) and keeps only stops at that station whose
    arrival and/or departure has already happened (a recorded/changed time is present) as of
    the moment of the call. A stop with no recorded time yet (still scheduled or forecast
    only) is skipped for the side(s) that haven't happened, matching the app's existing
    "arrival and departure are judged independently" rule.
  - For each realized stop, builds the same model input `predict-delay` and the browser build
    already build (`dbrel.ml.features`/`dbrel.ml.nn`), gets TabM's prediction from the current
    ONNX artifact, and gets the stored average the live app itself would show (the same
    `stop_delay_average`/`train_stop_delay_average` lookup `_delay_pred` uses) — not the
    bake-off's training-only baseline.
  - Scores both predictors against the real recorded delay with the existing metrics
    (`dbrel.ml.metrics`): MAE, pinball loss, band coverage, broken down by day, time-of-day/
    segment and holiday flag, the same shape the bake-off's report already uses.
  - Writes a study report (numbers + a short write-up) under `data/ml/studies/`, and does not
    touch `trip_stop`, the averages tables, or any trained artifact — this is read-only
    against the live API and the existing database.
- No existing behavior changes. This adds a new, self-contained capability; nothing in
  `delay-model`, the averages, or the frontend is modified.

## Capabilities

### New Capabilities
- `live-accuracy-study`: fetches fresh, already-realized stops from the DB Timetables API for
  a sampled set of stations, and reports how the current TabM model and the stored averages
  compare against those real outcomes.

### Modified Capabilities
(none)

## Impact

- New CLI command and its supporting module(s) under `src/dbrel/`; no changes to existing
  commands, the schema, or `main.py`/`urls.py`.
- Network: up to 3,000 new `/fchg` requests to the DB Timetables API, through the existing
  rate-limited `TimetablesClient` (same credentials, same token bucket `dbrel collect` uses).
  Read-only against DB Timetables; no write endpoints exist there.
- Local DB: read-only (stations, averages tables). No new tables; the report is a file, not a
  database write.
- Runtime: a single pass over 3,000 stations, rate-limited by the existing token bucket — this
  is a network-bound command, not a memory-heavy one, but per standing practice it still runs
  through `dbrel ml launch` (or an equivalent capped launch) rather than inline in a terminal
  session, since it can run for several minutes unattended.
