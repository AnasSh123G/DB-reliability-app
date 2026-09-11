# Implementation log

What was built, in what order, and why each decision went the way it did. The
README describes how to *use* the result; this describes how it came to exist and
what a future change would be arguing against.

State at time of writing: 2026-09-10, commit `47a82b1`.

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

### Still open

- Alembic is scaffolded with a 0001 baseline and an autogenerated 0002, but the 479 MB
  production database has **not** been migrated — deliberately, pending review of this pilot.
- `median_poll_gap_s` is not yet populated, so per-station `SETTLE_S` falls back to 300.
- Wings and number changes are parsed and `trip_link` exists, but nothing writes it yet.
- `/rchg` should be revisited for bandwidth (§19).
