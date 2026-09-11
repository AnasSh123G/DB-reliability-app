# Implementation log

What was built, in what order, and why each decision went the way it did. The
README describes how to *use* the result; this describes how it came to exist and
what a future change would be arguing against.

Written 2026-09-10 at commit `47a82b1`, describing the GTFS-RT design (§1–§10) and
the Timetables second source (§11). **§12 supersedes much of it:** the project became
an archival reader of recorded times, the GTFS-RT path went dormant, and several
claims below were measured wrong. Sections carry forward-pointers where that happened
— they are a log of what was believed at the time, not a current description.

---

## 1. Starting point

The repo contained `main.py` with three dataclasses — `Station`, `Train`,
`StationStop` — sketching a timetable, and nothing else. The goal: a local
database of German trains recording, for every stop of every run, both the
**supposed** and the **actual** arrival and departure time.

Two things were missing from the sketch and had to be added:

- **A dated dimension.** A `Train` is a timetable entry; only a *run on a
  particular day* can have an actual time. That became `Trip` / `TripStop`.
- **A source of actuals.** No feed states that a train departed. That gap drove
  the whole realtime + derivation design in §5–6.

## 2. Choosing a data source

Requirement: no API key, freely redistributable, covers long-distance and
regional rail.

[gtfs.de](https://gtfs.de) (DELFI-derived, CC BY-SA 4.0) was selected. It offers
static GTFS archives per market segment plus one nationwide GTFS-Realtime feed:

| Feed | URL | Size |
|---|---|---|
| `fv` long distance | `download.gtfs.de/germany/fv_free/latest.zip` | 0.4 MB |
| `rv` regional | `download.gtfs.de/germany/rv_free/latest.zip` | 12 MB |
| full (all modes) | `download.gtfs.de/germany/free/latest.zip` | 284 MB |
| realtime | `realtime.gtfs.de/realtime-free.pb` | 47 MB |

Alternatives with an API key (DB Timetables API, bahn.expert) were not pursued:
the constraint was a key-free, self-contained ingest.

## 3. Probing the feeds before writing the schema

The first schema draft assumed things about the feeds that turned out to be
false. Rather than discover this during ingest, two standalone probe scripts were
written — no database, no project imports, results cached under `data/probe/`:

- [scripts/probe_match.py](../scripts/probe_match.py) — id-space agreement,
  realtime coverage, stop structure, feed mechanics.
- [scripts/probe_validate.py](../scripts/probe_validate.py) — do matched
  `trip_id`s describe the *same journey* in both feeds, or is the match an
  integer collision?

Full results: [docs/probe-findings.md](probe-findings.md). Five findings changed
the design:

**a. The `station.dhid` natural key does not exist.** 0 of 16,345 `stop_id`s
match the IFOPT pattern `de:N:N`; they are opaque integers. Replacement key:
normalised name + coordinates rounded to 3 decimals (~110 m). Name alone
collides 6 times in `rv`; with coordinates it is unique.

**b. Realtime descriptors carry only `trip_id` + `start_date`.** Across all
76,898 trip updates, `route_id` and `start_time` are *always* empty. The planned
fallback matching on `(route, direction, start_time)` is therefore impossible,
and nothing unresolvable can be recovered — hence `rt_unmatched` as a visible
failure surface rather than a silent drop.

**c. The realtime feed is a rolling ~1-hour window**, not a service day. Matched
trips depart between −11.0 h and +0.7 h of the feed timestamp. Scored against all
trips active today, coverage looks like 14–19%; scored against trips *in progress
at the feed timestamp* — the only population the feed can cover — it is 85.6%
(fv 66.4%, rv 88.7%). This is why `poll` is one-shot and must be run repeatedly,
and why a trip's absence from one poll is evidence of nothing.

**d. `trip_id` joins are sound, and all gtfs.de feeds share one id space.**
204/204 shared fv trip_ids describe identical journeys. The rail trips in the
284 MB full feed are *exactly* `fv` + `rv` (43,417 trips) — so the full feed is
unnecessary and 12 MB buys identical rail coverage.

**e. Times past 24:00:00 are real.** Max observed `35:25:00`; 152,109 stop_time
values ≥ `24:00:00` in `rv`. Storing clock times as seconds-from-anchor is
required, not defensive.

Also measured: 92% of realtime trips are bus/tram/other, so filtering by a
prebuilt rail `trip_id` set before object construction keeps a poll cheap
(47 MB parses in 0.12 s); gzip is not offered, so every poll costs the full
47 MB; and `feed_info.feed_version` is the constant `latest-free`, so a content
hash is the only usable version identity.

## 4. Schema

Eleven tables, in three groups. Defined in [src/dbrel/models/](../src/dbrel/models/).

```
feed_version ──< route
     │            │
     └──< station_stop >── station        the planned pattern
              │              │
            train ──────────>│
              │
              ├──< trip_service_date       which dates a train runs
              │
              └──< trip                    one dated run
                     ├──< trip_stop >── station    supposed vs actual
                     └──< rt_observation >── rt_poll
```

**Core** ([models/core.py](../src/dbrel/models/core.py)) — `station`, `train`,
`station_stop`, `trip`, `trip_stop`.

**Provenance** ([models/meta.py](../src/dbrel/models/meta.py)) — `feed_version`,
`route`, `trip_service_date`.

**Raw realtime** ([models/realtime.py](../src/dbrel/models/realtime.py)) —
`rt_poll`, `rt_observation`, `rt_unmatched`.

Decisions worth defending:

- **Natural keys, not GTFS ids.** gtfs.de rebuilds the feeds roughly weekly and
  may re-mint ids. `station.natural_key` is normalised name + rounded coordinates;
  `train.natural_key` is product + operator + first departure + a hash of the
  exact station sequence. Departure time has to be in the train key or every
  train on an hourly line collapses into one; the cost is that a retimed train
  reads as a new train, which is arguably correct.
- **`station_stop` times are seconds from the service-date anchor**, not absolute
  instants — that is what makes `25:30:00` representable (finding *e*), and it
  keeps the planned pattern date-independent.
- **`trip_stop` copies its supposed times** rather than joining `station_stop`.
  The planned time of a run that already happened is a historical fact and must
  not change when next week's rebuild alters the pattern.
- **`trip` rows exist for every scheduled run; `trip_stop` rows only for observed
  ones.** Materialising the whole timetable would be ~1.5 M stop rows per day for
  trains nobody watched.
- **`rt_observation` is kept forever.** It is the only thing that makes a
  corrected derivation rule re-runnable (`finalize --force`) instead of a
  destructive one-way transform.
- **Indexes end in the measure** (`ix_ts_station_time_delay`,
  `ix_ts_hour_delay`), so the common aggregations are answered from the index.
  *(§12: renamed and extended. The rule was sound but applied incompletely — the
  arrival side had no index at all, and `is_handover`, which every honest station
  aggregate must filter on, was in none of them.)*
- **SQLite in WAL mode** ([db.py](../src/dbrel/db.py)) with
  `synchronous=NORMAL`, 128 MB cache, 256 MB mmap — WAL is what lets a poll write
  while a query reads; with WAL, `NORMAL` risks losing the last commit on a
  crash, not corrupting the file.

## 5. Ingest pipeline

### Static import — [gtfs/static_import.py](../src/dbrel/gtfs/static_import.py)

[gtfs/download.py](../src/dbrel/gtfs/download.py) fetches with
`If-None-Match` / `If-Modified-Since` (gtfs.de serves a strong ETag), caching to
`data/feeds/` with a sidecar `.meta.json`. A 304 is the normal outcome of
re-running within a week.

The import is **idempotent on content hash**: an identical archive already
imported as `active` is skipped. `--force`, or a previously failed attempt,
purges that feed version's rows (`_purge_feed_version`, newest dependency first)
and redoes it. Only one version per `kind` is `active`; older ones stay for
provenance as `superseded`.

Order of work: stops → routes → trips (rail-filtered) → stop_times → trains →
station_stops → calendar expansion → dated runs. Non-rail routes are dropped at
the `trips.txt` stage, before any stop_times are read. `--max-days` limits the
dated-run expansion, which is the only genuinely large write.

Two edge cases handled explicitly: **childless stops** referenced directly by
`stop_times` become their own station; and **two GTFS trips resolving to the same
train on the same date** keep the first and count the rest as `duplicate_runs`
(they are the same service, duplicated upstream).

### Time handling — [gtfs/timeutil.py](../src/dbrel/gtfs/timeutil.py)

Every absolute instant in the project is an INTEGER Unix epoch second. GTFS clock
times are converted against a **noon-minus-12-hours anchor**, not midnight. This
matters exactly twice a year: on 2026-03-29 Europe/Berlin skips 02:00–03:00 so
the service day is 23 hours long, and on 2026-10-25 it is 25 hours. Anchoring at
midnight would silently shift every time on those days by an hour. Noon is used
because it always exists and is never ambiguous.

### Realtime poll — [realtime/parser.py](../src/dbrel/realtime/parser.py), [realtime/poller.py](../src/dbrel/realtime/poller.py)

The parser is pure — bytes in, dataclasses out, no database. It takes a `keep`
predicate applied to each `trip_id` *before* stop updates are read, so the 92% of
the feed that is buses and trams never becomes objects.

A poll:

1. Builds the candidate service-date list from the fetch time — yesterday, today,
   tomorrow. A trip departing 23:50 is still running after midnight.
2. Loads `(gtfs_trip_id, service_date) → trip.id` for those dates and passes the
   trip-id set to the parser as its filter.
3. Resolves each descriptor by `(trip_id, start_date)`, falling back to trying the
   candidate dates if `start_date` names a date with no run. Unresolvable
   descriptors are upserted into `rt_unmatched` with a repeat-sighting counter.
4. **Writes an observation only when the value changed** since the last one stored
   for that `(trip, stop)`. A prediction republished unchanged every poll carries
   no information; storing it would be pure growth. Change detection uses
   `MAX(id)` per group — observations are append-only, so the highest id is the
   most recent.
5. Marks resolved runs `RUNNING` and bumps `n_polls_seen`.

A failed fetch is recorded as an `rt_poll` row with an `error`, not raised — a
failed poll is data. `--dry-run` reports match and change rates without writing.

`rt_poll` is the **listening record**, and it is what makes §6 possible at all.

## 6. Deriving "actual" times — [derive/finalize.py](../src/dbrel/derive/finalize.py)

> **§12:** this section still describes rule v1 exactly, and rule v1 still runs — but
> the path is now dormant. The claim "no source ever states that a train departed" is
> true only in the narrow sense that IRIS publishes no *flag*; a past event's `ct` is
> DB's recorded time, and reading it late needs none of the inference below.

The central problem: **the realtime feed publishes predictions, not verified
actuals.** No source in this project ever states that a train departed. So an
actual is defined as:

> the last published estimate for an event, observed after the event should have
> happened, that then stopped changing.

Because observations are stored only when a value *changes*, "stopped changing"
leaves no trace in the data. It has to be inferred from the absence of later
observations **combined with evidence that we were still polling** — which is
exactly what `rt_poll` provides. Without it, "the train was on time" and "nobody
was watching when it passed" are indistinguishable.

Rule v1 constants ([config.py](../src/dbrel/config.py)): `GRACE_S = 120` (an
event is considered to have happened this long after its predicted time),
`SETTLE_S = 300` (a value must then hold still this long to count as observed),
`SERVICE_DATE_CLOSE_S = 6 h`.

Per stop, the grade is:

| grade | condition | trusted |
|---|---|---|
| `OBSERVED` | revised at/after passage, then held while we were still polling | ✓ |
| `INFERRED_DOWNSTREAM` | the run was still reporting about later stops, so this value stood | ✓ |
| `LAST_SEEN` | the run left the feed before settling; its last word is carried | ✓ |
| `PREDICTION_ONLY` | no poll covered the moment it mattered; `poll_gap_s` records the blind spot | ✗ |
| `SKIPPED` | the feed said this stop was skipped | — |
| `NO_DATA` | no realtime information. **Never means "on time"** | ✗ |
| `MISSING` | scheduled, ran while we were polling, never appeared | ✗ |

`TRUSTED_QUALITIES` is exported so analysis code and the `stats` command filter
consistently; averaging a `PREDICTION_ONLY` guess in would quietly flatter every
number.

The most important behaviour here: **runs whose window no poll covered are left
untouched, not marked MISSING** (`_polled_between`). "The train did not run" and
"we were not looking" are different claims and only the first is a fact about the
railway. Marking the second as MISSING would poison every statistic derived from
the table.

Every row carries `rule_version`, so a corrected rule can be re-run over
`rt_observation` (`finalize --force`) and its output compared against the old.

## 7. CLI — [cli.py](../src/dbrel/cli.py)

Typer app, every command one-shot: it runs, reports, exits.

| Command | Does |
|---|---|
| `init-db` | create the schema |
| `import-static [--feed fv,rv] [--force] [--max-days N]` | download + import the timetable |
| `poll [--dry-run]` | one realtime fetch |
| `finalize [--date] [--force]` | observations → actual times |
| `stats` | row counts, feed versions, poll history, quality breakdown, punctuality |
| `unmatched` | realtime trips that resolved to no known train |

`poll` is deliberately not a daemon. Scheduling is the operator's call because
the bandwidth cost is real: no gzip, ~47 MB per fetch, so 5-minute polling is
~13 GB/day.

*(§11 added the `tt-*` commands; §12 added `show-train` and `show-station`, which are
how the log is actually read, and made every read command refuse to create a schema.)*

## 8. Current state of the local database

After one `import-static` (both feeds), three polls and one `finalize`:

| table | rows |
|---|---|
| `feed_version` | 2 (fv, rv — both `active`, covering 2026-09-05 … 2026-10-05) |
| `route` | 1,052 |
| `station` | 7,829 |
| `train` | 87,240 |
| `station_stop` | 1,194,867 |
| `trip` | 1,277,000 |
| `trip_stop` | 14,216 |
| `rt_poll` | 3 |
| `rt_observation` | 66,758 |
| `rt_unmatched` | **0** |

Database file: 502 MB. The three polls matched 6,253 / 6,255 / 6,289 rail runs
out of ~78,000 trip updates each, **with zero unmatched** — the `(trip_id,
start_date)` join holds in practice, as the probe predicted.

`trip_stop` is small because it is materialised only for observed runs, and only
three polls have happened; it grows with polling, not with the timetable.

## 9. Known limits

- **A single poll can never cover a full service day** (finding *c*). Building a
  day of actuals requires polling through the day.
- **Coverage is ~86%** of in-progress rail trips. fv scores lower (66%) because
  overnight long-distance services carry no realtime data.
- **There is no train number** in these feeds. `train_name` ("ICE
  Aachen → Köln") is constructed from product + origin + headsign, not from an
  official number, and `train_number` is always NULL.
- **Trip-level cancellations are not signalled.** Every descriptor arrives as
  `SCHEDULED`; cancellation is only visible per-stop as `SKIPPED`, or inferred
  from absence.
- **No automated tests yet.** Correctness currently rests on the probes plus the
  quality grades being conservative by construction. The obvious first targets
  are `timeutil` (DST anchors) and `_grade_stop`.
- Static feeds cover the next 30 days and are rebuilt roughly weekly.

## 10. If you change the ingest

Re-run the probes first. They exist to measure the feeds rather than trust them,
and every non-obvious decision above traces back to a numbered finding in
[docs/probe-findings.md](probe-findings.md).

---

## 11. Adding DB Timetables (IRIS-TTS) as a second source — 2026-09-10

### Why

§9 recorded "there is no train number" as a known limit. It is not fixable on the GTFS
side: `trips.txt` is `route_id,service_id,trip_id` and nothing else. The Timetables API
carries `tl@n` on 100% of stops, so it was added as a **second source**, not a
replacement — the end state is nationwide, and Timetables cannot cover Germany within 60
requests/minute.

### Three layers

| layer | source | scope | supplies |
|---|---|---|---|
| skeleton | gtfs.de static | nationwide, permanent | what should run; station coordinates; the name index; the traffic distribution |
| identity | Timetables `/plan` | tracked stations | train numbers, categories, lines, operators |
| observation | Timetables `/fchg` + GTFS-RT | tracked / nationwide | actual times, cancellations, platforms, delay causes |

GTFS is kept permanently because `/station/` returns no coordinates (so nothing else can
verify an EVA mapping), `ppth` gives routes as names only (so resolving them needs a
nationwide name index), and only GTFS says a train exists at all.

### What the probe changed before any schema was written

Findings §9–§19. Four results overturned a design decision:

- **§18 killed a grade.** `PLAN_HELD` was designed on the assumption that `/fchg` publishes
  only changes, so punctual trains never appear and their silence means "on time". 100% of
  planned events do appear, punctual ones as `ct == pt`. The grade was deleted.
- **§13 killed name matching as a join.** Only 72% of `ppth` names resolve to a gtfs.de
  station even punctuation-insensitively, and the failures are systematic (`Neuss Hbf` vs
  `Neuss Hauptbahnhof`, `Herford` vs `Herford, Bahnhof/ZOB`). The join goes through EVA.
- **§12 corrected the stop id.** The middle field is 10 digits, not the 6 the swagger prose
  claims, and `stopIndex` is 1-based.
- **§19 corrected a cost estimate.** `/fchg` averages 154 KB because it returns the whole
  horizon each time, so a tracked set costs ~3.5 GB/day at 30 s — not "a few MB".

### Identity

Two rules, both measured:

1. **The number is identity; the path is an attribute.** The GTFS-era key hashed the
   observed station sequence, which cannot survive a growing station set — going from 8 to
   300 stations would rehash every train through them. `(category, number)` had zero
   collisions over 1,108 pairs (§10), so it carries identity alone; `operator` is policed
   via `train_conflict` rather than keyed on.
2. **A name and a number are different entities.** `dp@l` is present on every regional stop
   and absent on every long-distance one (§11), so `train` and `service_line` are separate
   tables. "ICE 599" resolves to a train, "S1" to a line with 16 numbered trains.

`station_stop.stop_sequence` holds the true 1-based `stopIndex`, so sequences are sparse
but globally consistent — adding a station later fills a gap instead of renumbering.

### Derivation

`derive/finalize_tt.py` (rule_version 2) is separate from `finalize.py` so the GTFS path
stays byte-identical. Two real differences: the listening record is **per station**
(`tt_poll.station_id`), since one fetch is evidence about one station only; and
cancellations come from `cs='c'` + `clt` rather than being inferred, so
`trip_stop.cancelled` finally carries values. `_grade_stop` gained an optional `settle_s`
so a slowly-polled station cannot claim a value settled in 5 minutes.

### Pilot result (`data/test_database.sqlite`)

8 stations around Bochum Hbf, 100 sweeps at 30 s over 50 minutes:

- 248 Timetables trains, **100% with a real `train_number`**; 87,240 GTFS trains, 0%.
- 808 polls, 45,011 matched stop events, **2,972 stored (6.6%)** — the change filter working.
- 89 trips finalized, 160 stops, 1 cancellation, 477 delay-cause messages.
- One run stitched across up to 5 tracked stations into a single `trip`.
- `tt_unmatched` = 0; `train_conflict` = 0.

> **§12 read this pilot wrong on two counts.** The 6.6% figure was measured by
> `probe_timetables.py` comparing raw XML signatures in memory; the database round
> trip it never exercised leaked **33.6% duplicates**. And `tt_unmatched = 0` was not
> coverage — nothing wrote that table, and the `keep=` predicate discarded 80% of
> returned events before they could be counted. Neither number was evidence of what
> it was cited for.

### Still open

*(Superseded by §12's own list — kept as the record of what was outstanding here.)*

- Alembic is scaffolded with a 0001 baseline and an autogenerated 0002, but the 479 MB
  production database has **not** been migrated — deliberately, pending review of this pilot.
  → §12: both old files abandoned, a fresh one stamped at head.
- `median_poll_gap_s` is not yet populated, so per-station `SETTLE_S` falls back to 300.
  → §12: the per-station settle window is deleted; nothing settles anything now.
- Wings and number changes are parsed and `trip_link` exists, but nothing writes it yet.
  → still open.
- `/rchg` should be revisited for bandwidth (§19).
  → §12: **actively wrong for this design.** `/rchg` returns only the last two minutes,
  which is a low-latency optimisation. An archival reader wants the *widest* horizon it
  can get, and §20 measured that at ~15 hours.

---

## 12. The archival reader — 2026-09-12

### Why the design changed

The goal was restated, and the restatement invalidated the premise of §6: **a log of
what actually happened, for trend analysis and model training.** Recency is worth
nothing; completeness is worth everything. Two consequences followed immediately.

**Predictions are not acceptable output.** §6's `actual` is a *derived* value — the
last forecast that stopped moving — and no amount of grading turns a forecast into a
record. GTFS-RT cannot do better, because it publishes nothing else. IRIS can: it is
fed from DB's operational systems, so once a train has departed, `ct` holds the
realized time and stops moving. That is the value DB Navigator shows after departure.

**Tolerating latency removes the hard problem.** §6 had to infer "stopped changing"
from silence plus evidence of listening, because a value that stops changing leaves no
trace. Polling *after* the trains have gone deletes the inference: read the value once,
late, and it is already final. The whole settle-detection apparatus becomes unnecessary
rather than merely awkward.

So the GTFS-RT path (`rt_*`, `poll`, `finalize`) is left intact and **dormant** — rule
v1 still reproduces exactly — and IRIS becomes the only source of actuals.

### Three measurements that shaped it

Recorded in full as Part III of [probe-findings.md](probe-findings.md).

**§20 — `/fchg` retains passed events for ~15 hours, not 75 minutes.** §18 could only
establish a floor, because the pilot's watch ended before the API dropped anything.
Measured against the clock over 717 recorded values: the oldest was **905 minutes** old,
with a smooth taper and 89% under 8 hours old when read. Retention is an order of magnitude longer
than assumed, which is what makes reading late safe with enormous margin.

**A correction that reversed a decision: `/fchg` is a *delta*, so `/plan` is
mandatory.** The redesign was initially planned around dropping `/plan` entirely, on
the assumption that `/fchg` carries `pt` beside `ct` and could therefore supply both
halves. Measured across 2,462 stop elements: **89.7% carry no `pt` at all**, and only
10.9% of arrivals have one. `pt` and `<tl>` co-occur *exactly* — IRIS sends a full
record only for runs `/plan` cannot contain. So the planned side must come from
`/plan`, joined to `/fchg` by the stop id, which is verbatim identical and needs no
matching at all.

This matters because it relocates the binding constraint. It is not the rate limit —
21 stations cost ~3% of the daily budget — it is that **`/plan` expires**. §15: today
and tomorrow only, 404 for yesterday, and `_pending_hours` can look back at most two
hours. The roller must run hourly, forever, and a missed hour is unrecoverable.

**§21 — a per-station cost figure was off by 7×.** §19's "~2,170 stop events per
station per poll" was the per-*sweep* total across eight stations. Measured over 21:
**474 per station**, from 93 at Castrop-Rauxel to 1,119 at Köln Hbf.

### The realized-event filter

One rule, in [changes_poller.py](../src/dbrel/timetables/changes_poller.py):

```
realized  ⟺  (ct or pt) + GRACE_S  <  now
```

**Judged on `ct`, never `pt`.** A train booked for 10:00 and running thirty minutes
late has not departed at 10:15; grading it against its planned time would invent a
departure that never occurred. The planned time is only a fallback for when IRIS has
said nothing at all.

Arrival and departure are filtered **independently**. A stop whose arrival has passed
but whose departure has not contributes its arrival now and its departure on a later
poll — the two arrive as separate observations and the later one carries both.

A stop that is past but for which IRIS published no time is skipped: there is nothing
to record, and writing a row of NULLs would misrepresent silence as data.

### `QUALITY_RECORDED`, and what absence now means

`OBSERVED` asserts something specific — *revised after passage, then held while we were
still polling*. A single late read satisfies the arithmetic of that test while
satisfying none of its meaning, so awarding it would have been a lie. The archival path
gets its own grade:

| grade | condition | trusted |
|---|---|---|
| `RECORDED` | read after the event had passed; `ct` is DB's recorded time | ✓ |
| `CANCELLED` | `cs='c'` — stated outright, bypasses grading entirely | — |
| `NO_DATA` | we *were* reading that station across the window and nothing came | ✗ |
| `PREDICTION_ONLY` | no poll covered the window — our blind spot, not the railway's silence | ✗ |

`read_lag_s` replaces `poll_gap_s` as the confidence measure for a recorded value: the
seconds between the event and our reading of it. A longer lag means more certainty the
value had finished moving.

The last two grades are the reason the per-station listening record still earns its
keep. Distinguishing them uses `_polled_between` — "was there a poll *inside* the
window" — rather than `_polled_at_or_after`, which asks only whether our last-ever poll
of that station was later than the instant. At 30-second cadence the weaker test was
harmless; at hourly cadence it is nearly always true and therefore says nothing.

The window is `[passage + GRACE_S, passage + TT_LINGER_S]`, with `TT_LINGER_S = 6 h` —
well inside the measured 15-hour retention, and set equal to `SERVICE_DATE_CLOSE_S` so
the period we check for coverage is exactly the period over which grades may still
change. It was initially 75 minutes, on §18's floor; raising it to the measured value
reclassified a `PREDICTION_ONLY` row as `NO_DATA`, which is the evidence being used
properly rather than discarded.

`_settle_for` and its per-station `SETTLE_S` are deleted. They existed to stop a
slowly-polled station claiming a value settled in five minutes; nothing settles
anything now.

### Idempotency by digest, not by tuple comparison

The change-detection filter had drifted into **three different column orders** — the
row written, the row read back, and the value compared:

- written: `arr_platform = (cp or pp)`; compared: `cp` alone. `pp` is present on
  virtually every stop and `cp` only on real platform changes, so the two were almost
  never equal.
- the table has one `cancelled_at_utc`; the comparison tuple has two slots, and the
  loader put the single value in both.

Both mismatches re-fired every sweep, because the comparison set was reloaded from the
database each time. Measured on the pilot: **999 of 2,972 rows (33.6%) byte-identical
to their predecessor**, and ten events carrying one row per sweep. The damage was worse
than storage — `observed_at` became "the last time we rewrote an unchanged value",
which is precisely the quantity `_grade_stop` reads as "when it was last revised".

Rather than repair the comparison, it is deleted. `tt_observation` gains a `value_sha`
digest of the published value and a `UNIQUE (trip_id, stop_sequence, value_sha)`
constraint, and writes go through `ON CONFLICT DO NOTHING`. **A plain `UNIQUE` over the
value columns would not have worked**: SQLite treats NULLs as distinct, and most of
those columns are legitimately NULL — an origin stop has no arrival.

The poller also filters candidates against the digests already stored, so the reported
"stored" count is honest rather than a `rowcount` SQLAlchemy cannot reliably supply for
an executemany on SQLite.

Since only realized values are ingested and a realized `ct` is stable, re-reading the
same event across the 15-hour horizon is now a no-op. `trip_stop` is likewise upserted
on `uq_tripstop_trip_seq`, because a run is re-graded on every pass until it closes and
each read may add stops the previous one had no value for.

Consequence: `pp` and `cp` are no longer conflated. The **planned** platform belongs to
the schedule (`station_stop.platform_code`) and the **changed** platform to the
observation, which is what lets `trip_stop.planned_platform` and `changed_platform`
carry real values instead of the same coalesced field twice.

### Defects found and fixed

| defect | effect | fix |
|---|---|---|
| `keep=` predicate gated parsing on a known run key | **178,000 of 223,011 pilot events (80%)** discarded inside the parser, before an object existed | predicate dropped; nothing dies at parse time |
| `n_unmatched` branch unreachable — both sides consulted the same dict | `SUM(n_unmatched) = 0` across 808 polls while 178,000 events vanished | counted after a real lookup |
| `tt_unmatched` never written by anything | the "/plan roller has fallen behind" alarm did not exist; `tt_unmatched = 0` was cited in §11 as success | upserted per event, accumulating `n_polls` |
| finalize required `supposed_arrival_utc IS NOT NULL` | a run whose only tracked stop is its origin was excluded **forever** — 55 of 248 pilot trips (22%) | coalesces departure when there is no tracked arrival |
| `train_conflict` did `continue`, skipping the trip write | a conflicting run got no trip, so `/fchg` could never match it and the day's actuals were lost silently | parked on a variant identity `…#<path>`, as the docstring always claimed |
| `finalized_at` stamped on a wall clock 420 s after the last stop | froze `NO_DATA` rows while values were still in the horizon, needing `--force` to revisit | stamped only once `SERVICE_DATE_CLOSE_S` has passed |
| `--force` delete sat inside `if all_rows` | a forced re-run yielding nothing left stale rows behind | moved outside the guard |
| `stats` called `create_all()` | a read command mutating the schema; a typo'd `DBREL_DB` left 24 empty tables | removed; read commands call `_require_schema` and say "run init-db first" |

### One identity module, two importers

Both importers now create runs, and if they minted identities differently the same
service would become two trains. The identity, DST and schedule-writing logic therefore
moved to [timetables/runs.py](../src/dbrel/timetables/runs.py) — `upsert_run`,
`upsert_line`, `fix_dst`, `sec_of_day`, `tt_feed_version` — and `plan_import.py` calls
it rather than keeping a second copy.

`upsert_run` also stopped loading all 87,240 GTFS identity keys per sweep; only `tts:`
keys can ever match, since `train_identity_key` is source-prefixed precisely so the two
sources cannot collide.

### `/fchg` creates the runs `/plan` cannot

The 10% of entries carrying `<tl>` + `pt` are additions and replacements — exactly what
`/plan` structurally never contains, and exactly the ones IRIS sends complete. Those are
now built on the fly through the same `upsert_run`, with bus and tram categories
rejected since the project is rail only.

In the first real sweep this recovered **118 runs** that the old `keep=` predicate would
have discarded before an object existed.

### Schema changes

- `QUALITY_RECORDED` added, and added to `TRUSTED_QUALITIES`.
- `trip_stop.read_lag_s` — confidence measure for a recorded value.
- `tt_observation.value_sha` + `UNIQUE (trip_id, stop_sequence, value_sha)`.
- `ix_ts_station_time_delay` → **`ix_ts_station_dep`**, now
  `(station_id, supposed_departure_utc, is_handover, dep_delay_s)`.
- **`ix_ts_station_arr`** — new. The arrival side was entirely unindexed, and a station
  board is half arrivals.
- `is_handover` added before the measure in the aggregate indexes. Every honest
  station-level average must exclude handover stops, and reading that flag from the
  table defeated the index-only property those indexes exist for.
- `ix_ts_quality` → `ix_ts_quality_delay`, so the punctuality pass is index-only.
- **`ix_ts_station_stop`** — new. `station_stop_id` is a nullable FK that was unindexed,
  so pattern row → dated instances meant a full scan of the fact table.
- `ix_ss_station` extended to `(station_id, feed_version_id, planned_departure_sec)`.
  Leading column unchanged so bare station lookups still seek, but a caller can now
  scope to one feed instead of summing every rebuild's copy of the same pattern.

No migration was written. Both existing files were abandoned — `dbrel.sqlite` had no
`alembic_version` row at all and was 13 tables behind; `test_database.sqlite` was
stamped `0001` while carrying the `0002` schema; and both migrations are empty `pass`
stubs, so there was nothing to migrate *from*. A fresh file was created with
`create_all()` and stamped at head, which is what makes future migrations possible.

### Reading the log — [cli.py](../src/dbrel/cli.py)

Neither query existed. `search` printed a one-line summary and did not even project
`SearchAlias.station_id`, so a station alias would have matched and printed nothing —
academic, since nothing ever writes station aliases.

| command | does |
|---|---|
| `show-train <number\|name> --date D` | every stop of one run, scheduled against actual |
| `show-station <name\|eva> --date D` | everything that called at one station that day |

Both take `--format tsv`, emitting raw epochs beside the formatted times so a training
script never parses display output. There was no export of any kind before this — no
CSV, no JSON, no views.

`show-train` needed no new index: `uq_trip_train_date` serves the seek and
`uq_tripstop_trip_seq` supplies both the join and the ordering, by accident of the
unique constraints.

`show-station` **converts the date to a UTC window**, which is the whole point. Filtering
on `trip.service_date` reads every stop ever recorded at that station and discards
almost all of it; converting to instants lets the query seek. Confirmed by
`EXPLAIN QUERY PLAN`:

```
MULTI-INDEX OR
 |--SEARCH ts USING INDEX ix_ts_station_dep (station_id=? AND supposed_departure_utc>? AND <?)
 `--SEARCH ts USING INDEX ix_ts_station_arr (station_id=? AND supposed_arrival_utc>? AND <?)
```

`unmatched` now reports the Timetables side first, since that is the one that means
planned data is being lost permanently.

### The tracked set is a corridor, not the busiest stations

Times exist only where you poll, so a set chosen purely by traffic yields fragmentary
journeys — `tt-rank-stations` near Essen returns mostly Köln and Düsseldorf S-Bahn halts,
which few trains visit consecutively. The set is instead the Rhein-Ruhr corridor: Köln →
Düsseldorf → Duisburg → Oberhausen → Essen → Bochum → Dortmund → Hamm, plus the
Wuppertal/Hagen/Witten southern route and the northern Ruhr branch (Gelsenkirchen,
Herne, Wanne-Eickel, Castrop-Rauxel, Recklinghausen, Bottrop) and the western reach
(Krefeld, Neuss, Solingen). 21 stations; `stop_sequence` sparseness means it can grow
without renumbering anything.

Auto-resolution got 17 of 21. **All four failures were §13 cases** and needed `--manual`
(§22): DELFI's comma suffix on `Hamm (Westf), Hauptbahnhof`, IRIS's extra hyphen in
`Herne-Wanne-Eickel Hbf`, a `/` in `Köln Messe/Deutz` producing HTTP 404, and
`Neuss Hbf` vs `Neuss Hauptbahnhof`.

Two things worth carrying forward. `Hamm (Westf)` **does** exist as a parent station in
the free feeds, contradicting §13's claim that it and Münster have none. And a fresh
instance of the §14 trap: `/station/KNE`, a plausible guess at Neuss's DS100, returns
**Neuwied** — a valid in-range rail eva for entirely the wrong city. Neuss is `KN`,
eva 8000274. A DS100 result must be checked against the returned *name*, never trusted
because the eva looks well-formed.

### First real capture

21 stations, one `/plan` roll of 84 station-hours and one `/fchg` sweep:

| | |
|---|---|
| stops returned | 9,950 (mean 474/station) |
| matched to a `/plan` trip | 1,486 |
| runs created from `/fchg` alone | **118** |
| forward events correctly skipped | 768 |
| values stored | 717 |
| **future values leaked** | **0** |
| **duplicate rows** | **0** |
| `tt_unmatched` | 8,464 |

Then `tt-finalize`: 214 trips, 268 stops — `RECORDED` 233, `NO_DATA` 26, `CANCELLED` 9.

The 8,464 unmatched is not noise: it is `/plan` having four hours of coverage against a
fifteen-hour `/fchg` horizon, and it should fall sharply once the roller has run a full
day. It was previously invisible.

### Operational shape

```
15 * * * *  uv run dbrel tt-import-plan     # mandatory, expiring — a missed hour is gone
40 * * * *  uv run dbrel tt-poll            # reads what has already happened
30 4 * * *  uv run dbrel tt-finalize
0  4 * * 1  uv run dbrel import-static      # national skeleton, weekly
```

Cost at 21 stations: roughly 2,000 requests/day against a 72,000/day budget. The
schedule accumulates day by day, because IRIS knows only today and tomorrow; a
month-ahead schedule is available only from GTFS, which would reintroduce the
cross-source join this design exists to avoid.

### Still open

Resolved from §11: `SERVICE_DATE_CLOSE_S` is now load-bearing; Alembic is stamped
against a live database; `tt_unmatched` is written.

- **`station.eva_confidence` is inverted and never holds what it documents.** The column
  comment says "km between coordinates"; the code writes `0.0` for DS100-resolved and
  `1.0` for name-resolved, so the most trustworthy mappings score lowest on a field
  named *confidence*, and no distance is ever computed. `eva_verified_at` is declared
  and never written.
- `median_poll_gap_s` is still unpopulated, but no longer load-bearing — nothing derives
  a settle window from it any more.
- `trip_link`, `train_xref` and `train_merge` remain declared and unwritten. `train_xref`
  is the interesting one: measured feasibility of matching a GTFS train to an IRIS train
  is ~60–75%, with the residue **structurally biased** toward busy hubs and
  high-frequency lines. Build it as validation, never as a join.
- `gtfs_neighbours` truncates to `[:5000]` with arbitrary set ordering and assumes
  *dense* stop sequences, while Timetables sequences are deliberately sparse. It will
  start dropping evidence non-deterministically as the tracked set grows.
- `TT_SWEEP_LOCK` is defined and unused, so two overlapping cron sweeps are not prevented.
- **Still no automated tests.** The archival path was verified with throwaway scripts
  against scratch databases — realized filter, four grades, upsert idempotency, conflict
  variants — none of which are committed. The obvious first targets are `_has_happened`,
  `_value_sha`, `upsert_run`, and `timeutil`'s DST anchors.
