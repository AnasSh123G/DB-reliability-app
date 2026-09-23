# Design

## Context

See `proposal.md` — Why, for motivation. The constraints that shape the approach:

**The archive is byte-identical to what we already parse.** Each archive row carries
`timestamp`, `url`, `api_name` (`timetables/v1/plan` or `timetables/v1/fchg`),
`response_data` (the verbatim XML), `status_code`, and `year`/`month`/`day` partition keys.
`parser.parse_timetable(data: bytes, keep)` already handles both endpoints — including
`<tl>`, `<ar>`/`<dp>`, `ps="a"` additions, `<ref>` replacements, and `<m>` messages — and
`runs.upsert_run()` already mints identities. So this is a **replay**, not a second
ingest. Writing a parallel parser would duplicate the grading rules that are the entire
reason for preferring raw over processed data.

**Grading hangs off one variable.** `changes_poller.run_tt_poll()` opens with
`now = int(time.time())`, threads it into `RunContext(now=now)`, and
`_has_happened(event, now)` decides observation with `reference + GRACE_S < now`. Replay
correctness reduces almost entirely to making `now` the archive row's `timestamp`.

**The existing poll loop is station-driven.** `run_tt_poll()` selects tracked stations,
then calls `client.changes(station.eva)` per station, writing a `TtPoll` row per fetch.
The archive is response-driven: a partition file holds whatever responses the upstream job
happened to capture. The two orders have to be reconciled.

**Nothing in the venv can read parquet.** No pyarrow, pandas, duckdb, or polars.
`pyproject.toml` already sets the precedent for keeping heavy optional machinery out of the
base install (the lightgbm note), and `src/dbrel/ml` is the precedent for a module nothing
else may import.

**Archive cadence differs by an order of magnitude.** Upstream polls `/fchg` every ~6h;
`collect` polls every 15m. Finding §20 puts `/fchg` retention at ~15h, so a 6-hourly poll
does generally catch events post-hoc — but `poll_gap_s` will be far wider and the
`NO_DATA` / `PREDICTION_ONLY` share far higher than live collection produces.

## Goals / Non-Goals

**Goals:**

- Reuse `parse_timetable()` and `upsert_run()` unmodified, so backfilled and live rows are
  graded by one implementation.
- Make the replay's notion of "now" explicit and per-response, so forecasts are rejected
  by the existing rule rather than a new one.
- Keep the parquet dependency quarantined behind an optional extra.
- Make acquisition resumable and bounded; a 30-day window must not become a 35 GB download.

**Non-Goals:**

- No change to how `collect`, `tt-poll`, or `tt-import-plan` behave when run live. The
  `now` parameter defaults to wall clock.
- No station discovery from the archive. `germany_stations.toml` and `tt-map-stations`
  remain the only way a station becomes tracked (findings §13/§14/§22/§25 are why).
- No use of `monthly_processed_data/`. It is not a fallback; its delay column is the defect
  being avoided.
- No upstream contribution to fix their merge. Out of scope here.
- Nothing about the ML pipeline. That redesign is gated on a separate conversation.

## Decisions

### 1. A replay source implementing the client's surface, not a new ingest path

`ArchiveReplaySource` exposes `plan(eva, date, hour)` and `changes(eva)` returning the same
`Response(status, body, elapsed_ms, headers)` dataclass, served from an in-memory index of
one day-partition's rows. `import_plan()` and `run_tt_poll()` then run against it with no
knowledge that they are not talking to HTTP.

*Alternative rejected:* a standalone `archive_ingest.py` calling `parse_timetable()` and
building fact rows itself. It would have to re-derive `_has_happened`, `_resolve`,
`_is_full_record`, `_load_patterns` and the `NO_DATA`/`PREDICTION_ONLY` logic — the exact
code whose correctness is the point of this change. Two copies would drift.

### 2. `now` becomes an injected parameter — the load-bearing decision

Add `now: int | None = None` to `run_tt_poll()` and `import_plan()`, resolving to
`int(time.time())` when omitted. In replay, `now` is the `timestamp` of the archive
response being served.

This is what makes the spec's forecast requirement fall out of existing code rather than
new code: an archived `ct` for an 04:00 stop published by a 00:57 poll fails
`reference + GRACE_S < now` at `now = 00:57`, so it is not credited as observed. A later
15:20 poll re-offers the same stop and does pass. That is precisely dbrel's live semantics,
replayed.

*Alternative rejected:* monkeypatching `time.time()` during replay. Invisible in call
sites, hostile to tests, and breaks the moment any code reads the clock for another reason.

*Alternative rejected:* leaving `now` as wall clock and filtering forecasts separately.
Every archived event is months past under wall clock, so `_has_happened()` returns true
unconditionally and every forecast grades `RECORDED`. This reintroduces the exact defect
described in `proposal.md`.

### 3. Replay is driven by archive sweeps, in chronological order

Group a day partition's `/fchg` rows into **sweeps** by their `timestamp` (upstream's
per-station calls within one job land within a few minutes of each other — observed
`00:54`–`00:58` and `15:20`–`15:25`). Replay one sweep at a time, in order, calling
`run_tt_poll()` once per sweep with `now` set to that sweep's median timestamp and the
replay source scoped to that sweep's responses.

`/plan` responses are replayed first for the day, through `import_plan(date=…, hours=…)`,
because `/fchg` is a pure delta for ordinary runs (89.7% of stops carry no `pt`) and the
planned side must exist before changes can attach.

Ordering matters for supersession: `upsert_run()` sees the earlier forecast sweep before
the later recorded one, which is the same order live collection sees them in.

*Alternative rejected:* replaying all of a day's `/fchg` at once with a single `now`. It
would collapse the forecast/recorded distinction that decision 2 exists to preserve.

### 4. Station scoping stays with the database; a missing station is skipped, not created

The replay source only answers for EVAs the database already tracks. An archived EVA with
no tracked station is counted and skipped. This keeps the `tt_unmatched` discipline intact
and avoids the name-resolution failures documented in §13/§14/§22/§25 — the archive's
`station_name` field is upstream's own resolution, which we have no reason to trust over
our EVA-keyed seed.

Measured overlap: 5,255 of 5,422 seeded EVAs; 210 of 212 Ruhr stations.

### 5. Acquisition: Hugging Face HTTP with a local manifest

Download `raw_data/year=Y/month=M/day=D/*.parquet` over plain HTTPS resolve URLs into
`data/archive/`, recording each file's expected size and upstream `oid` in
`data/archive/manifest.json`. A file matching its manifest entry is reused. No
`huggingface_hub` dependency — the tree API and resolve URLs are stable and already
exercised during investigation.

Window: default last 30 days; `--from`/`--to` override. Acquisition extends one day either
side of the window, matching upstream's own `process_month.sh` rationale for cross-midnight
runs.

Sizing from measured partitions (112–176 MB/day): ~3.5–5.5 GB for 30 days.

*Alternative rejected:* `hf download` via `uv run --with`. Convenient interactively, but it
makes the backfill depend on a CLI shelling out, with no manifest of its own to resume from.

### 6. Parquet reading behind a `backfill` extra, `pyarrow` only

`pyproject.toml` gains `backfill = ["pyarrow>=18"]`. Only `src/dbrel/ingest/archive.py`
may import it, and it reads `response_data`/`timestamp`/`url`/`api_name`/`status_code`
column-wise, one file at a time, never a whole month.

*Alternative rejected:* duckdb. Upstream uses it for the SQL merge we are deliberately not
performing; we need row iteration, which pyarrow does with a smaller install.

*Alternative rejected:* adding it to base dependencies. pyarrow is ~40 MB; the
`pyproject.toml` comment about not making a collector box carry numpy/scipy applies
verbatim.

### 7. Timestamps converted explicitly at the boundary

Archive timestamps are naive Europe/Berlin. `parse_ts()` already handles IRIS's
`%y%m%d%H%M` inside the XML, so stop times need nothing new. Only the archive row's own
`timestamp` column needs conversion to a UTC epoch for use as `now`, done once in
`archive.py` via `zoneinfo`. `runs.fix_dst()` continues to handle the ambiguous autumn
hour for stop times.

### 8. Reporting is part of the deliverable

The command reports, per run: sweeps replayed, stops written by quality grade, archived
EVAs skipped as untracked, tracked stations the archive never covered, and **hours missing
from the archive** within the window. The last one is not incidental — hours 05–07 of
2026-08-28 are simply absent, and upstream documents 196 missing hours across 2025-11 to
2026-07. An operator must be able to see a hole rather than infer it from a thin day.

## Risks / Trade-offs

- **Adding `now` to two live functions could change live behaviour** → Default to
  `int(time.time())` and assert in a test that omitting it reproduces current grading on a
  fixed fixture. The parameter is threaded, never branched on.

- **Wide poll gaps make backfilled data visibly weaker than live data** → Accepted and
  surfaced, not smoothed. `poll_gap_s` and `quality` already express it, and the run
  reports the grade histogram so the difference is legible rather than buried. This is the
  honest form of the same information the processed parquet fabricates.

- **Higher `NO_DATA` share may surprise, looking like the backfill failed** → Report grade
  counts prominently and document the expected shape. A 26.6% never-reported rate is a
  property of the archive, not a bug in the replay; the alternative is a fabricated zero.

- **Sweep grouping by timestamp proximity is a heuristic** → Derive sweeps by clustering on
  a gap threshold well under the ~6h cadence rather than a fixed window, and pin the
  clustering with a test over a real partition. Mis-grouping degrades to a slightly wrong
  `now` within one sweep, which at worst mis-grades stops within minutes of the boundary.

- **Replaying thousands of sweeps × thousands of stations could be slow** → Replay is
  CPU/SQLite-bound with no rate limit to respect, so it can run flat out. Index each
  partition once into memory, batch identity resolution per sweep the way `import_plan()`
  already batches per station set, and make the window resumable per day so a long run can
  be stopped and continued.

- **Upstream could change schema, paths, or disappear** → The manifest records what was
  downloaded, and `data/archive/` is retained, so an existing backfill stays reproducible.
  Column names are read by name with an explicit error if absent, rather than positionally.

- **Overlap with live-collected rows could downgrade good data** → `upsert_run()`'s existing
  value-comparison path governs this; the tasks include an explicit test that replaying a
  window already covered live neither duplicates rows nor replaces a `RECORDED` stop with a
  weaker grade.

- **`dbrel.sqlite` growth** → 30 days national is a large multiple of the current corridor
  database. Measure on a single day first (task 3) and report the projection before running
  the full window, so the disk cost is known before it is incurred rather than after.

- **Known residual: same-day train-number reuse can merge two unrelated trips** →
  `upsert_run()`'s trip resolution treats a second run_key found under an existing
  `(train_id, service_date)` as a later observation of the SAME physical trip ("refreshed
  in place" -- see the comment above that branch), which is false when an operator reuses
  one train number for two genuinely different workings on the same calendar day (found
  live: S 8976 ran both a short Dachau-only local and a long Erding-Altomünster through
  service on the same date). The second working's `path_sha1` overwrites the first's on the
  Trip row, so `finalize_tt` gap-fills the first trip with stations from the SECOND
  working's schedule -- the first trip's own real, correctly-attributed `RECORDED`
  observations are untouched, but it gains extra `PREDICTION_ONLY` rows for stops it never
  called at. Measured prevalence: 41 of 252,362 trips (0.016%) in a 6-day pilot. Accepted
  as a documented residual for now: `stop_delay_average` filters to `quality='RECORDED' AND
  cancelled=0` (`derive/stop_averages.py`), so PREDICTION_ONLY noise from this defect cannot
  reach the averages feature. A full fix needs trip-level scoping (e.g. by `daily_trip_id`
  or `path_sha1`) analogous to the station_stop variant scoping this change already does,
  and is out of scope here.

## Migration Plan

No schema migration. The backfill writes existing tables through existing code; `quality`,
`poll_gap_s`, `read_lag_s` and `rule_version` already exist and already carry the needed
semantics.

Rollout is incremental and each step is independently checkable:

1. Land `now` injection with live behaviour pinned by test. No user-visible change.
2. Land acquisition (`dbrel archive-fetch`) — downloads only, writes nothing to the database.
3. Land replay for a single day behind `--dry-run`, reporting the grade histogram without
   committing.
4. Replay one real day into a scratch database; compare its grade distribution against the
   ~26.6% never-reported expectation and against a live-collected day for the same corridor.
5. Open the window to 30 days.

Rollback: the feature is additive and opt-in. Backfilled rows are identifiable by
`rule_version` together with `tt_poll` sweep provenance, so a bad run can be deleted
without touching live-collected rows; `reset-db --yes` remains the blunt option, and
`data/archive/` survives it so no re-download is needed.

## Open Questions

- Whether backfilled stops should be distinguishable from live ones by an explicit column
  rather than inferred from poll provenance. Deferrable: it changes no requirement in the
  spec, and can be added as a nullable column later if querying proves awkward.
- Whether to extend the window past 30 days once the per-day disk and runtime cost is
  measured. The window is already a parameter, so this is an operational choice, not a
  design one.
