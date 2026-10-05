# Proposal

## Why

The deployed database (`scripts/build_slim_db.py`, the copy the Fly.io server reads) is 1.7 GB,
and most of it is repetition. Measured on 2026-10-05 on that copy:

- **The schedule repeats itself.** `station_stop` already has no dates: it holds one row per stop
  for every train and every route variant (5.54 M rows, about 880 MB with indexes). Many trains and
  variants run the same stop pattern shifted in time, for example an hourly RE with the same
  stops, platforms and travel times. Counting every stored field, there are 742,134 variants but
  only 213,575 distinct patterns (fv 2,599, rv 27,033, tt 183,943). Storing each pattern once
  needs 1.78 M stop rows instead of 5.54 M (−68%).
- **The run history is kept only to choose a route variant.** The copy holds the last 60 days of
  `trip` (2.46 M rows, about 324 MB). The server reads it for two things: a train's most recent
  variant, and each variant's last run date and run count. Because only 60 days are kept, the
  count can disagree with the collection database and pick a different variant (seen on
  2026-10-05). A summary of one row per train and variant answers both questions exactly, over
  every run.

Day-type templates (default, Saturday, Sunday, holiday) were also measured and rejected. One
default variant per train covers 80.1% of the 13.6 M runs, and day types raise that only to 82.3%,
which leaves 2.4 M exceptions. Variants follow construction periods and short runs, not the day
of the week.

A smaller file is cheaper to keep and faster to upload to the volume. It also lets the deployed
server answer exactly as the collection database would.

## What Changes

- **Shared stop patterns in the deployed database.** Each distinct stop pattern is stored once:
  the stop order, stations, platforms, source stop ids, pickup and drop-off types, and times
  relative to the pattern's start. Each train variant stores only its pattern, its start time and
  its rows' ids as a few runs of consecutive ids. A view named `station_stop` rebuilds exactly
  the rows the server reads today, ids included, so the server's schedule queries need no
  change.
- **A route-variant run summary instead of `trip`.** The deployed copy no longer holds per-day
  runs. A table with one row per train and variant (last run date, the last run's id on that date
  and the number of runs, over all history) replaces them. The two queries that read `trip` use the
  summary when it is present and `trip` otherwise, and give the same answers either way.
- **Further lossless compaction (design D8).** The deployed copy stores:
  - each route hash and each route path once, with numbers in their place elsewhere
  - two-column-keyed tables without a second key copy
  - origin and destination names once

  It drops the build-only and collector-only structures. `train` and the run summary become views
  that give back every column.
- **The build checks itself.** The builder rebuilds every stop row from the patterns and compares
  them with the collection database, and refuses to finish on any difference. It reports the file
  size and the row counts.
- **Responses match the collection database.** For a fixed sample of stations, trains and lines,
  the server's API responses from the deployed copy are identical to those from the collection
  database, `station_stop_id` row ids included.

Out of scope:
- The 48 GB collection database, the collector, the derive steps and the training pipeline are
  unchanged.
- Day-type templates (measured above).
- Compacting the `train` table (about 140 MB) or the search aliases.

## Capabilities

### New Capabilities
- `deployed-database`: the read-only copy of the collection database that a deployed server
  reads. It covers what the copy contains, how the schedule and route-variant history are stored,
  how the build verifies itself, and that the server answers from it as it would from the
  collection database.

### Modified Capabilities
(none)

## Impact

- **Code:**
  - `scripts/build_slim_db.py`: new pattern, variant and run-summary tables, the `station_stop`
    view, the verification and the size report.
  - `src/dbrel/web/queries.py`: the two `trip` reads (`train_route`'s variant choice and
    `_variant_per_train_at`) go through one helper that reads the summary when present.
- **Data:** the deployed file shrinks from 1.7 GB to an estimated 0.5–0.6 GB. That figure is an
  estimate from per-row sizes, and task 3.1 measures it. The collection database is only read.
- **API:** none. Responses, row ids included, stay as they are.
- **Deployment:** the Fly.io volume and upload steps are unchanged, and the uploaded file is
  smaller.
