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
uv run dbrel collect --every 4h --for 7d   # cold start, then a cycle every 4 hours for a week
```

One command runs the whole pipeline: schema, GTFS import, the tracked stations
(`config/ruhr_stations.toml` — 212 stations within 70 km of Bochum Hbf, ranked by traffic),
the schedule, the actuals, and the averages — repeating on a timer until the duration
elapses or you press Ctrl-C (see [Collecting continuously](#collecting-continuously)
below). `uv run dbrel reset-db --yes` deletes the database to start over, without
re-downloading the cached GTFS feeds.

<details>
<summary>Or step by step, if you want to run one stage at a time</summary>

```bash
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

</details>

Then query it:

```bash
uv run dbrel show-train "NX 89715" --date 2026-09-12
uv run dbrel show-station "Essen Hbf" --date 2026-09-12 --format tsv
uv run dbrel search "Bochum"                          # trains, lines and stations
```

### The web UI

```bash
uv run dbrel seed-demo          # optional: plausible past runs, so there is something to show
                                 # before dbrel collect has accumulated any of its own
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

An average is shown from its very first sample, not hidden until there are several — a
real number, even a noisy one, beats a page of em-dashes for the first few days of a fresh
collection. Below 5 samples it is marked **provisional**; that marking clears on its own as
`dbrel collect` accumulates more runs. `seed-demo` is entirely optional and untouched by any
of this: it exists only to make the UI demonstrable before real data exists, seeds strictly
before the earliest real day so it can never mix with or displace it, and `dbrel seed-demo
--clear` removes it cleanly.

Django owns no database. `DATABASES = {}` leaves it on the dummy backend, so it cannot
write a table into `dbrel.sqlite` even by accident; all reads go through the SQLAlchemy
layer via `read_scope()`, which never commits.

### Collecting continuously

```bash
uv run dbrel collect \
  --every 4h \                       # cycle interval: 4h, 90m, 1h30m (default 4h)
  --for 7d \                         # total run time: 7d, 12h, 30m; '0' = until Ctrl-C
  --lead-hours 3 \                   # how far ahead /plan is pulled each cycle
  --max-lookback 16h \               # how far back a cycle's /plan window may reach
  --stations config/ruhr_stations.toml \  # the tracked corridor (default, shown for clarity)
  --cold-start                       # schema, GTFS import, station seed, aliases, verify,
                                      # and a one-off 0-23h /plan backfill, only if needed

uv run dbrel collect --once          # a single cycle — the shape a cron slot wants
uv run dbrel reset-db --yes          # drop the database; keeps data/feeds (~11 MB) and data/probe
```

Each cycle runs `/plan` import → `/fchg` poll → finalize → averages, and commits each phase
independently, so a failure partway through never rolls back work already done. Ctrl-C
finishes the phase in progress and prints a summary instead of a traceback; a second Ctrl-C
aborts immediately. Only one `collect` runs at a time (`data/tt_sweep.lock`); a second
instance — including a `reset-db` while one is running — refuses with the first one's pid.

**Why the /plan window is computed explicitly instead of trusted to the rolling importer.**
`tt-import-plan` with no `--date` rolls forward from a per-station high-water mark, but its
own lookback is clamped to two hours — fine for an hourly cron, unsafe at any slower
cadence, because the hours in between are silently never fetched and the `/fchg` events for
them land in `dbrel unmatched` instead of a trip. `collect` sidesteps this: each cycle
computes the hour buckets from the previous cycle back to `--max-lookback`, forward to
`--lead-hours`, and imports them explicitly. Re-covering an hour that already succeeded
costs nothing — a 200 is never re-fetched — so the overlap is free insurance, not overhead.

**Why 4 hours.** `/fchg` itself retains a value for about 15 hours (§20), so nothing measured
is ever lost. The real ceiling is `TT_LINGER_S` (6 hours): past it, a stop that produced no
value is graded "we weren't listening" instead of "nothing happened yet". 4 hours leaves
margin for a slow cycle. `--max-lookback` (default 16h) is capped independently, because
`/plan` itself purges past hours of *today* after roughly 14 hours (§24) — reaching further
back than that buys nothing and, unlike a 200, a 404 is retried every cycle, so an
unnecessarily wide window is a standing cost rather than a one-time one.

**Cron is still an option** if you would rather not leave a foreground process running:

```cron
7  * * * *  cd /path/to/repo && uv run dbrel tt-import-plan     # hourly, mandatory
23 * * * *  cd /path/to/repo && uv run dbrel tt-poll && uv run dbrel tt-finalize
```

Here the two-hour lookback clamp is safe because the cadence is hourly. Polling hourly is
generous on its own terms too: a passed event lingers in the `/fchg` horizon for about 15
hours, so nothing is lost by reading long after the fact. At the current 212-station
corridor the two jobs together cost under 5% of the 72,000/day request budget.

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
| `tt-finalize [--service-date D] [--force]` | Grade the stops that produced no value; roll each run up |
| `collect [--every 4h --for 7d] [--stations FILE] [--once]` | Run plan → poll → finalize → averages on a timer; cold-starts an empty database |
| `reset-db [--yes]` | Delete the database; keeps `data/feeds` and `data/probe` |
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
- **TODO: a one-shot full-day `/plan` backfill cannot be scaled to all of Germany at all,
  not just slowly** — confirmed live on the 212-station backfill (§26): major hubs 404'd
  during morning rush hour because their target hour had already aged past `/plan`'s ~14h
  same-day retention (§24) by the time the backfill reached them, not because no train ran.
  At Germany's ~8,215 rail stations (§6) a full-day sweep needs ~65.7h at 50 rpm — over 4×
  the 14h retention window — and no reordering fixes it: even sweeping hour-outer instead
  of station-outer, the math (`(h+1)×2.74h ≤ h+14h`) only holds through hour ≈6, so a flat
  national sweep can never capture more than the first 6-7 hours of any day. The same
  ceiling hits the *recurring* cycle too (an 8h rolling window across 8,215 stations costs
  ~21.9h/cycle, vs. the 6h `TT_LINGER_S` budget). Real fix, not yet done: never backfill
  history at national scale — only ever request a short near-term lookahead, continuously,
  and extend `poll_priority` tiering (already live in `changes_poller.py`) into
  `plan_import.py` so most of Germany's stations are checked far less often than a curated
  set of hubs/corridors.
- **TODO: ~40% of candidate stations fail automatic EVA resolution once minor stops are
  in scope** — 94 of 238 candidates within 40 km of Bochum Hbf never got an eva and are
  simply untracked (§25). Two concrete, not-yet-done fixes: (1) expand the Rhein-Ruhr city
  abbreviations DELFI uses (`D-`, `DO-`, `DU-`, `BOT `, `OB `, `GE `, `ME-`, …) as another
  `query_variants` fallback — mechanical, no manual work, likely recovers a meaningful
  chunk on its own; (2) for whatever's left, a human looks up the correct eva by hand and
  assigns it with `tt-map-stations --manual <station_id>=<eva>`. Regenerate
  `config/ruhr_stations.toml` afterward either way, so the fix is permanent.
