# Proposal

## Why

dbrel's database grows only as fast as it polls: the schedule cannot be prebuilt (IRIS
knows today and tomorrow only), so every hour not captured is permanent data loss. The
national seed in `config/germany_stations.toml` lists 5,422 stations, but the database
holds history only for the window during which `dbrel collect` has actually been running.

The [deutsche-bahn-data](https://github.com/piebro/deutsche-bahn-data) project has been
archiving the *same* IRIS `/plan` and `/fchg` responses, verbatim, since 2024-07 — and for
all German stations since 2025-11. Its raw archive covers 5,255 of our 5,422 seeded EVAs
(97%) and 210 of the 212 Ruhr corridor stations. That is history we cannot otherwise
obtain at any price, because it is already past.

We take it from the **raw** archive, not from the project's published
`monthly_processed_data` parquet, because that parquet asserts on-time running for stops
where nothing was ever measured. Its merge falls back
`COALESCE(f.departure_change_time, p.departure_planned_time, …)`, so a stop with no `/fchg`
report is published as `delay_in_min = 0`. Measured against the raw archive over 161,871
stops that had a guaranteed post-event poll, **26.6% are such fabricated zeros**, varying
by operator from 1.5% (NWB) to 55.6% (replacement buses) — an unevenness no downstream
correction can undo. Our own `NO_DATA` grade exists precisely because absent data never
means punctual (finding §18: `/fchg` publishes punctual trains explicitly, as `ct == pt`).
The raw archive still carries that distinction; the processed parquet has destroyed it.

## What Changes

- **New `dbrel backfill` command** that replays archived IRIS responses through the
  existing ingest path, producing `trip`/`trip_stop` rows indistinguishable in kind from
  live-collected ones — same `RULE_VERSION_TT`, same four quality grades, same
  `tt_message` delay causes.
- **Archive download** of `raw_data/` day partitions from the Hugging Face dataset into
  `data/archive/`, resumable and content-addressed, with no API credentials required.
- **A replay source** offering the same surface as `TimetablesClient` (`plan()`,
  `changes()`), serving bytes recorded in the archive instead of issuing HTTP.
- **`now` becomes injectable** in `run_tt_poll()` and the grading path. This is the
  load-bearing change: `_has_happened()` compares against `now`, and a replay must judge
  each response against **the instant that response was recorded**, not wall-clock time.
  Without it every archived event looks long past, and IRIS *forecasts* — 20.7% of `ct`
  values in a sampled window were polled before their event — would be graded `RECORDED`.
  This is the same defect we are declining to inherit from the processed parquet.
- **Default window is the last 30 days**, bounded and configurable, not the full archive.
  That is ~3.5–5.5 GB rather than ~35 GB, and it covers the 22-day gap the upstream
  monthly releases structurally cannot fill (they publish on the 2nd, for the prior month).
- No change to `dbrel collect`. The collector keeps running and keeps sole responsibility
  for the present; the archive only supplies the past.

## Capabilities

### New Capabilities

- `archive-backfill`: Acquiring archived IRIS `/plan` and `/fchg` responses from a public
  third-party mirror and replaying them through dbrel's ingest so that historical stops
  enter the fact table with the same provenance, quality grading and delay-cause records
  as live collection — and so that a stop the archive never observed is recorded as
  unobserved rather than as punctual.

### Modified Capabilities

None. This is the project's first spec; `openspec/specs/` is currently empty, so the
ingest behaviour `archive-backfill` depends on is not yet described by a spec of its own.

## Impact

**Code**

- New: `src/dbrel/ingest/archive.py` (download + manifest), `src/dbrel/ingest/replay.py`
  (the replay source), a `backfill` command in `src/dbrel/cli.py`.
- Modified: `src/dbrel/ingest/changes_poller.py` and `src/dbrel/ingest/plan_import.py` gain
  an optional `now` parameter, defaulting to `int(time.time())` so live behaviour is
  untouched.
- Reused unchanged: `parser.parse_timetable()` already parses both endpoints, and
  `runs.upsert_run()` already mints identities. The archive's `response_data` is the same
  XML our client fetches, which is why this is a replay and not a second parser.

**Dependencies**

- Reading the archive's parquet needs a columnar reader; the project currently has none
  (no pyarrow, pandas, duckdb, or polars). This must go in a new optional extra rather
  than the base dependency set, for the reason `pyproject.toml` already states about
  lightgbm: a collector box should not be made to carry it. Nothing outside the backfill
  module may import it.

**Data**

- `data/archive/` holds downloaded day partitions; must be gitignored.
- `dbrel.sqlite` grows by the backfilled stops. `NON_RAIL_CATEGORIES` already excludes
  `Bus`/`STR`/`STB`/`U`, so the archive's worst-covered categories are filtered before a
  row is written.

**Known limits, stated rather than discovered later**

- The archive polls `/fchg` roughly every 6 hours. `/fchg` retains passed events for ~15
  hours (§20), so most events do get a post-event reading — but poll gaps far exceed
  `collect`'s 15 minutes, so backfilled stops will carry wider `poll_gap_s` and a higher
  share of `NO_DATA`/`PREDICTION_ONLY` than live-collected ones. That is the honest
  outcome, and it is queryable via `quality`.
- The archive has real holes: hours 05–07 of 2026-08-28 are absent entirely, and upstream
  documents 196 missing hours between 2025-11 and 2026-07. These become
  `PREDICTION_ONLY` with a measured `poll_gap_s`, not silent zeros.
- Archive timestamps are naive Europe/Berlin; dbrel stores UTC epoch seconds. Conversion
  must be explicit and DST-aware.
