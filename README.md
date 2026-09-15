# dbrel — German train reliability database

A local SQLite database of German long-distance and regional trains recording, for every
stop of every run, both the **supposed** and the **actual** arrival/departure times — the
times DB Navigator shows *after* the train has gone.

Built for trend analysis and model training, not for a departure board. Recency does not
matter; completeness does.

Two sources, and they do different jobs:

- **DB Timetables / IRIS-TTS** (`/plan` + `/fchg`) — the whole database. Real train
  numbers on 100% of stops, and recorded times rather than forecasts. Needs an API key.
- **[gtfs.de](https://gtfs.de)** (DELFI-derived, CC BY-SA 4.0) — the reference skeleton:
  station names, coordinates and route patterns. No key required. It is *not* a join
  target: GTFS carries no train numbers at all, so nothing in it ever joins to a
  recorded actual.

## Quick start

```bash
uv sync --extra web
uv run dbrel init-db
uv run dbrel import-static                            # names + patterns (one-off, ~20s)
uv run dbrel build-aliases                            # make stations searchable
uv run dbrel tt-map-stations --station-ids 274,111    # pick the stations to watch
uv run dbrel tt-import-plan                           # the schedule
uv run dbrel tt-poll                                  # the actuals
uv run dbrel tt-finalize                              # account for what produced nothing
uv run dbrel compute-averages                         # per train, per station
uv run dbrel stats
```

Then query it:

```bash
uv run dbrel show-train "NX 89715" --date 2026-09-12
uv run dbrel show-station "Essen Hbf" --date 2026-09-12 --format tsv
uv run dbrel search "Bochum"                          # trains, lines and stations
```

### The web UI

```bash
uv run dbrel seed-demo          # optional: plausible past runs, so there is something to show
uv run dbrel serve              # http://127.0.0.1:8000
```

Three pages:

- **Search** — one box over stations, lines and trains. Type `Bochum`, `RE1` or `NX 26811`.
- **Station board** (`/station/<id>/`) — everything arriving and departing in a time
  window, with the average delay that train runs *at that station* underneath it.
- **Train route** (`/train/<id>/`) — the whole journey as IRIS publishes it, with the
  scheduled arrival/departure and the typical lateness at each stop we monitor. Stops
  outside the tracked corridor are shown but marked, so the route is never silently
  truncated to the three stations we happen to watch. A line (`/line/<id>/`) lists its
  numbered runs.

While too little real data has accumulated, the pages say so in a banner and every figure
is marked simulated; they switch to measured data on their own once enough samples exist.

Django owns no database. `DATABASES = {}` leaves it on the dummy backend, so it cannot
write a table into `dbrel.sqlite` even by accident; all reads go through the SQLAlchemy
layer via `read_scope()`, which never commits.

### Running it for real

Two cron jobs. `tt-import-plan` is the one that must not slip: **`/plan` serves only today
and tomorrow**, and only a rolling ~14 hours of today — past that, a station-hour bucket is
purged and the planned data is gone for good (§24).

Starting fresh mid-day, take the partial backfill once before the cron takes over; it
recovers about fifteen hours the 3-hour roller would never reach:

```bash
uv run dbrel tt-import-plan --date $(date +%F) --hours 0-23
```

The buckets past the retention window return 404, which is counted as `buckets_missing`
and looks exactly like a genuinely empty hour — so a clean run is not evidence the whole
day was captured.

```cron
7  * * * *  cd /path/to/repo && uv run dbrel tt-import-plan     # hourly, mandatory
23 * * * *  cd /path/to/repo && uv run dbrel tt-poll && uv run dbrel tt-finalize
```

Polling hourly is generous: a passed event lingers in the `/fchg` horizon for about 15
hours, so nothing is lost by reading long after the fact. At 21 stations the two jobs
together cost ~3% of the 72,000/day request budget.

## Commands

| Command | Does |
|---|---|
| `init-db` | Create the schema |
| `import-static [--feed fv,rv] [--force]` | Import the GTFS skeleton; idempotent on content hash |
| `tt-check [--pattern X] [--burst N]` | Verify credentials, report the rate limit; says *which* header is wrong |
| `tt-rank-stations [--near X --km N] [--limit N]` | Rank stations by traffic they would bring in; pure SQL, no API calls |
| `tt-map-stations --station-ids 1,2 [--manual id=eva]` | Resolve stations to EVA numbers and start tracking them |
| `tt-verify-stations [--min-overlap 0.6]` | Behavioural check: do IRIS's neighbours match GTFS's? |
| `tt-import-plan [--date D --hours 5-22] [--force]` | Import planned timetables; rolls forward hourly by default |
| `tt-poll [--interval S --minutes M]` | Read `/fchg` and record every event that has already happened |
| `tt-finalize [--date D] [--force]` | Grade the stops that produced no value; roll each run up |
| `build-aliases` | Index every station for search; run once after `import-static` |
| `compute-averages [--basis real\|synth\|all] [--full]` | Average each train's delay at each station into `train_station_delay` |
| `seed-demo [--days N] [--clear]` | Invent plausible past runs so the UI has something to show |
| `serve [--host H --port P]` | Run the web UI |
| `show-train <number> --date D [--format tsv]` | Every stop of one run, scheduled against actual |
| `show-station <name> --date D [--format tsv]` | Everything that called at one station on one day |
| `search "ICE 599"` | Find a train by number, a line by label (`S1`, `RE4`), or a station |
| `stats` | Row counts, poll history, quality breakdown, punctuality |
| `unmatched` | `/fchg` events with no schedule — the alarm that the roller has fallen behind |

Credentials go in `.env` as `DB_CLIENT_ID` and `TIMETABLES_API_KEY`. Both headers are
required; either alone returns 401. Free tier is 60 requests/minute, shared across
processes by a lock-file token bucket.

## The data model

Six tables carry the data; one is a derived rollup; four record how it was obtained; two
are provenance.

```
station ──< station_stop >── train        the constant schedule
   │              │            │
   │              │            └──< trip                 one dated run
   │              └───────────────────< trip_stop >──────┘
   │                                       supposed vs actual, per stop
   └──< train_station_delay >── train      how late it usually is, here
service_line ──< train                    'RE 4', 'S1'
```

- **`station`** — a place. Keyed on normalised name + rounded coordinates, because
  gtfs.de stop ids are opaque integers with no documented stability. Also carries the
  IRIS `eva`, and whether we poll it.
- **`train`** — a recurring service. For IRIS, identified by its number; for GTFS, by
  product, operator, departure time and exact station sequence.
- **`station_stop`** — one stop in a train's planned pattern. Times are seconds from the
  service-date anchor, which is what makes `25:30:00` representable. **This is the
  constant schedule** and it does not change once written.
- **`trip`** — one dated run: links a train to a date, carries journey-level timing.
- **`trip_stop`** — **the fact table**: supposed vs actual, per stop, with a quality
  grade. This is what you query.
- **`train_station_delay`** — a derived rollup: mean, median, p90 and punctuality for one
  train at one station. Rebuildable from `trip_stop` at any time, which is why it lives in
  `models/aggregates.py` rather than beside the domain. Keyed on `(station, train, basis)`,
  where `basis` is `REAL` or `SYNTH` — measured and simulated samples never share a row, so
  no figure is ever a blend of the two. Not keyed on `service_line`: `upsert_line` only
  mints a line when both category and line are present, and no long-distance stop carries
  `dp@l` (§11), so every ICE and IC would be dropped.
- **`tt_message`** — delay causes attached to a stop. The most useful label in the
  database for training.

Recording how it was obtained: `tt_poll` (which station we read, when — the listening
record), `tt_plan_fetch` (which schedule buckets are already captured), `tt_unmatched`
(events we hold no schedule for). Provenance: `feed_version`, `route`, `search_alias`.

Supposed times are **copied into `trip_stop`**, not joined from `station_stop`: the
planned time of a run that already happened is a historical fact and must not change when
next week's feed rebuild alters the pattern.

## What "actual" means here

**It means actual.** IRIS's `ct` is an estimate before a train passes and DB's recorded
time afterwards, and the ingest only ever reads events that are already in the past:

```
realized  ⟺  (ct or pt) + 120s  <  now
```

Judged on `ct`, never `pt`. A train booked for 10:00 and running 30 minutes late has not
departed at 10:15, and grading it against its planned time would invent a departure that
never occurred. Arrival and departure are filtered independently, so a stop whose arrival
has happened but whose departure has not contributes only its arrival — the rest arrives
on a later poll.

Because a value that has already happened never moves again, there is nothing to infer
and no revision history to keep. Four grades:

| grade | meaning |
|---|---|
| `RECORDED` | Read after the event had passed. DB's recorded time. `read_lag_s` says how long after. |
| `CANCELLED` | IRIS said so outright (`cs='c'` plus a timestamp). Measured, not inferred. |
| `NO_DATA` | We were polling this station while the value was still readable, and nothing appeared. **Never means "on time"** — punctual running is published explicitly as `ct == pt`. |
| `PREDICTION_ONLY` | No poll covered the window. Our blind spot, not the railway's silence. `poll_gap_s` says how wide. |

Only `RECORDED` carries a time, and only `RECORDED` should enter an average.

## Analysing it

Plain SQL over `trip_stop`. Filter on quality — the other grades have no time at all:

```sql
-- punctuality by station (DB counts under 6 minutes as on time)
SELECT s.name, COUNT(*) n, AVG(ts.dep_delay_s)/60.0 mean_min,
       1.0*SUM(ts.dep_delay_s < 360)/COUNT(*) pct_on_time
FROM trip_stop ts JOIN station s ON s.id = ts.station_id
WHERE ts.quality = 'RECORDED' AND ts.dep_delay_s IS NOT NULL AND ts.is_handover = 0
GROUP BY s.id HAVING n >= 20 ORDER BY mean_min DESC;

-- how one train has behaved over time
SELECT t.service_date, t.final_delay_s/60.0 min_late, t.status
FROM trip t WHERE t.train_id = ? ORDER BY t.service_date;

-- delay causes, ranked
SELECT m.category, COUNT(*) FROM tt_message m GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

-- worst trains at one station, straight off the rollup
SELECT tr.display_name, d.n_dep, d.avg_dep_delay_s/60.0 mean_min, d.pct_dep_punctual
FROM train_station_delay d JOIN train tr ON tr.id = d.train_id
WHERE d.station_id = ? AND d.basis = 'REAL' AND d.n_dep >= 5
ORDER BY d.avg_dep_delay_s DESC LIMIT 20;
```

The first query is what `compute-averages` precomputes into `train_station_delay`, one row
per train per station, so the web board does not recompute it per request.

`show-train` and `show-station` both take `--format tsv` for piping straight into a
training script. Seeded runs are excluded from `show-station` unless you pass
`--include-synthetic`, and a synthetic `show-train` TSV carries a `#` warning line.

## Probes

`scripts/probe_match.py` and `scripts/probe_validate.py` measure the feeds rather than
trusting them. Findings are written up in [docs/probe-findings.md](docs/probe-findings.md)
— worth reading before changing the ingest, and worth re-running if the feeds change
shape.

## Notes and limits

- **Times exist only at stations you actually poll.** `/fchg` gives a run's whole route
  from one sighting, but only as *names*. Pick a **corridor** rather than the busiest
  stations: consecutive stops are what make a journey whole, and what let you see *where*
  a delay was incurred rather than only that it existed.
- **The schedule cannot be prebuilt.** IRIS knows today and tomorrow only, so it
  accumulates day by day and a missed `/plan` window is permanent data loss.
- **`/fchg` sends a pure delta** for ordinary runs — 89.7% of stops carry no `pt` at all
  — so `/plan` is mandatory, not an optimisation. The two join on the stop id verbatim,
  with no name resolution and no tolerance window.
- The ~5% of runs `/plan` structurally cannot contain (additions and replacements) are
  exactly the ones `/fchg` sends complete, with `<tl>` and `pt` together. Those are
  created on the fly.
- **Station names do not match between the two sources.** Only 72% of IRIS `ppth` names
  resolve to a gtfs.de station even with punctuation-insensitive matching, so the join
  between sources goes through EVA numbers, never names. See `docs/probe-findings.md` §13.
- **Timetables cannot cover Germany at 60 rpm** (~5,400 stations would need ~518k
  requests/day against an 86k budget). It covers a tracked subset, which grows by command
  rather than by migration — `stop_sequence` is sparse, so adding a station fills a gap
  instead of renumbering any history.
