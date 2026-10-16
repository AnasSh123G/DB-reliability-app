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
cp .env.example .env   # then fill in DB_CLIENT_ID and TIMETABLES_API_KEY -- see below
uv run dbrel collect --every 15m --for 7d  # cold start, then a cycle every 15 minutes for a week
```

Credentials come from https://developers.deutschebahn.com: register, create an app, then
subscribe that app to the "Timetables" product. `.env` is gitignored, so this is the one
thing a fresh clone needs before anything else works — `dbrel tt-check` verifies both
headers and reports which one is wrong, without ever printing them. The rate dbrel actually
requests at is not fixed at whatever the product page advertises — see
[Request rate](#request-rate) below, or `src/dbrel/config.py` directly.

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
`dbrel collect` accumulates more runs.

Django owns no database. `DATABASES = {}` leaves it on the dummy backend, so it cannot
write a table into `dbrel.sqlite` even by accident; all reads go through the SQLAlchemy
layer via `read_scope()`, which never commits.

### Collecting continuously

```bash
uv run dbrel collect \
  --every 15m \                      # cycle interval: 15m, 4h, 1h30m (default 15m)
  --for 7d \                         # total run time: 7d, 12h, 30m; '0' = until Ctrl-C
  --lead-hours 3 \                   # how far ahead /plan is pulled each cycle
  --max-lookback 16h \               # how far back a cycle's /plan window may reach
  --derive-every 4 \                 # run finalize+averages every Nth cycle (default 4)
  --start-at-midnight \              # wait until the next local midnight before starting
  --stations config/ruhr_stations.toml \  # the tracked corridor (default, shown for clarity)
  --cold-start                       # schema, GTFS import, station seed, aliases, verify,
                                      # and the first 0-23h /plan backfill, only if needed

uv run dbrel collect --once          # a single cycle — the shape a cron slot wants
uv run dbrel reset-db --yes          # drop the database; keeps data/feeds (~11 MB) and data/probe
```

**`--start-at-midnight`.** `--for` measures wall-clock runtime from launch, not calendar
dates — starting `--for 7d` at 21:00 gives a partial evening, six full days, and a seventh
that stops mid-afternoon. `--start-at-midnight` waits (Ctrl-C-able, like everything else)
until the next local 00:00 before cold start runs at all, so the day cold start backfills —
and every day after it for the rest of `--for` — is a whole calendar day. The wait is not
charged against `--for`; that clock still starts only once cold start begins.

```bash
uv run dbrel reset-db --yes
uv run dbrel collect --every 15m --for 7d --start-at-midnight
```

Each cycle runs **daily backfill → `/plan` import → `/fchg` poll → finalize → averages**, and
commits each phase independently, so a failure partway through never rolls back work already
done. Ctrl-C finishes the phase in progress and prints a summary instead of a traceback; a
second Ctrl-C aborts immediately. Only one `collect` runs at a time (`data/tt_sweep.lock`); a
second instance — including a `reset-db` while one is running — refuses with the first one's pid.

**The daily backfill.** The first cycle of each calendar day pulls that whole day's schedule —
all 24 hours for every tracked station, 5,088 calls, about a minute — before anything is
polled against it. That ordering is not cosmetic: `/fchg` sends a bare *changed* time for an
ordinary run, with no planned time attached, so a stop whose planned row is missing is a value
that has to be thrown away. It is gated on work actually done rather than a "last run" marker
(`tt_plan_fetch` has a row per bucket attempted, whatever the status), so it costs one count
query on the other 95 cycles of the day, resumes correctly after a crash mid-sweep, and heals
a day left partial by a 404 while that day is still inside `/plan`'s ~14 h window.

**Why finalize and averages run every 4th cycle.** Neither makes an API call, and neither can
produce anything the poll before it did not already store — on a 15-minute cycle they would
simply repeat thousands of rows of work 96 times a day. Backfill and poll still run every cycle.

**Why the /plan window is computed explicitly instead of trusted to the rolling importer.**
`tt-import-plan` with no `--date` rolls forward from a per-station high-water mark, but its
own lookback is clamped to two hours — fine for an hourly cron, unsafe at any slower
cadence, because the hours in between are silently never fetched and the `/fchg` events for
them land in `dbrel unmatched` instead of a trip. `collect` sidesteps this: each cycle
computes the hour buckets from the previous cycle back to `--max-lookback`, forward to
`--lead-hours`, and imports them explicitly. Re-covering an hour that already succeeded
costs nothing — a 200 is never re-fetched — so the overlap is free insurance, not overhead.

**Why 15 minutes.** One `/fchg` sweep is one call per tracked station — about 4 seconds for
212 of them — so the cadence is chosen for how promptly a passed event is recorded, not for
cost. `/fchg` retains a value for about 15 hours (§20), and the real ceiling is `TT_LINGER_S`
(6 hours): past it, a stop that produced no value is graded "we weren't listening" instead of
"nothing happened yet". 15 minutes sits far inside both. Going below ~2 minutes buys nothing:
`GRACE_S` means an event is not recordable until 120 s after it passes.
`--max-lookback` (default 16h) is capped independently, because
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

### Backfilling history from the public archive

`collect` can only record the present — the schedule cannot be prebuilt, so every hour not
captured is gone. But someone else has been recording the same IRIS responses all along.
[piebro/deutsche-bahn-data](https://github.com/piebro/deutsche-bahn-data) has archived the
raw `/plan` and `/fchg` payloads four times a day since 2024-07, and for **every** German
station since 2025-11. It covers 5,255 of this project's 5,422 seeded EVAs (97%) and 210 of
the 212 Ruhr corridor stations.

```bash
uv sync --extra backfill                       # adds pyarrow; nothing else needs it
uv run dbrel archive-fetch --from 2026-08-24   # download partitions (~112-176 MB/day)
uv run dbrel backfill --from 2026-08-24        # replay them into the database
```

Both default to the last 30 days. `archive-fetch` writes nothing to the database and is
resumable — a partition already held intact is reused. `backfill` records each day it
finishes, so an interrupted run continues rather than restarting; `--restart` redoes the
window, `--dry-run` grades and reports without writing.

No API key is needed for either. The replay feeds the archived bytes through the same
`parse_timetable` → `upsert_run` → `finalize` path as live collection, so backfilled stops
carry the same `rule_version`, the same four quality grades and the same `tt_message` delay
causes. Each archived sweep is judged against **the instant it was recorded**, not against
now, so IRIS's forecasts are still rejected as forecasts — see `read_clock` in
`ingest/timeutil.py`.

**Only `raw_data/` is used, never the project's `monthly_processed_data/` parquet.** That
parquet is convenient and wrong in a way that cannot be undone downstream. Its merge falls
back

```sql
COALESCE(f.departure_change_time, p.departure_planned_time, ...) AS departure_change_time
```

so a stop that `/fchg` never reported is published with its *planned* time as its actual,
and therefore `delay_in_min = 0`. Measured against the raw archive over 161,871 stops that
had a guaranteed post-event poll, **26.6% of those zeros were never measured at all** —
and the rate runs from **1.5%** (NWB) to **55.6%** (replacement buses), so no constant
correction fixes it. §18 is why it is wrong rather than merely pessimistic: `/fchg`
publishes punctual trains *explicitly*, as `ct == pt`, so silence is absence of data, not
evidence of punctuality. That is exactly what `NO_DATA` means here, and the distinction
survives only in the raw responses.

**Expect a lot of `NO_DATA` and `PREDICTION_ONLY`.** The archive polls every ~6 hours
against `collect`'s 15 minutes. `/fchg` retains passed events for ~15 hours (§20), so most
events do still get a post-event reading — but poll gaps are far wider, and around a
quarter of scheduled stops were never reported. `backfill` prints the grade histogram at
the end so this is visible rather than inferred. Only `RECORDED` rows carry a time, and
only `RECORDED` should enter an average — the same rule as for live data.

The archive also has real holes: hours 05–07 of 2026-08-28 are simply absent, and upstream
documents 196 missing hours between 2025-11 and 2026-07. Both commands report the hours
they could not cover, and a hole becomes a measured `poll_gap_s` rather than a silent zero.

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
| `collect [--every 15m --for 7d] [--stations FILE] [--once]` | Run daily backfill → plan → poll → finalize → averages on a timer; cold-starts an empty database |
| `archive-fetch [--from D --to D] [--days N]` | Download archived IRIS responses from the public mirror; resumable, no API key |
| `backfill [--from D --to D] [--dry-run] [--restart]` | Replay downloaded partitions into the database; resumes per day |
| `reset-db [--yes]` | Delete the database; keeps `data/feeds` and `data/probe` |
| `build-aliases` | Index every station for search; run once after `import-static` |
| `compute-averages [--full]` | Average each train's delay at each station into `train_station_delay` |
| `compute-stop-averages [--full]` | Average each train's delay at each of its stops into `train_stop_average` |
| `build-passenger-lines` | Regroup trains into the lines passengers know (one RE4 per region, however operators spell it); the web's line pages and search use these. Rerun after new data |
| `serve [--host H --port P]` | Run the web UI |
| `show-train <number> --date D [--format tsv]` | Every stop of one run, scheduled against actual |
| `show-station <name> --date D [--format tsv]` | Everything that called at one station on one day |
| `search "ICE 599"` | Find a train by number, a line by label (`S1`, `RE4`), or a station |
| `stats` | Row counts, poll history, quality breakdown, punctuality |
| `unmatched` | `/fchg` events with no schedule — the alarm that the roller has fallen behind |

Credentials go in `.env` as `DB_CLIENT_ID` and `TIMETABLES_API_KEY`. Both headers are
required; either alone returns 401.

### Request rate

The settings below live in `src/dbrel/config.py` (`TT_RPS_FAST`, `TT_RPM_PENALTY`,
`TT_PENALTY_S`, `TT_FETCH_WORKERS`), each overridable by the matching `DBREL_TT_*` env var.
That file is the one source of truth for the current rate — nothing else in this repo sets
or duplicates it.

The gateway advertises 60 requests per **minute** in `x-ratelimit-limit`, but measurement
says it does not enforce it: 1,500 sequential calls at ~44/s and 300 `/fchg` calls at
~2,560/min both drew zero 429s, with `x-ratelimit-remaining` pinned at `0` throughout
without ever blocking.

So the limiter is adaptive rather than pessimistic, and degrades on evidence:

| | rate | when |
|---|---|---|
| fast (default) | `DBREL_TT_RPS_FAST` = 60/**second** | normally |
| penalty | `DBREL_TT_RPM_PENALTY` = 58/**minute** | for `DBREL_TT_PENALTY_S` = 180 s after any 429 |

The penalty lives in the shared lock file, so a 429 seen by one process slows every
process, and it expires on its own without needing a restart. If DB ever switches
enforcement on, that is a three-minute slowdown instead of a wall of failures.

One connection tops out near 45/s on this API's ~22 ms round trip, so `/plan` buckets are
fetched through a small pool (`DBREL_TT_FETCH_WORKERS`, default 8) to reach the permitted
rate. Only the HTTP fetch is concurrent — parsing and every database write stay on the
calling thread. A full-day, 212-station backfill is 5,088 calls: about **1 minute**.

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
  train at one station **on one day of the week**. Rebuildable from `trip_stop` at any
  time, which is why it lives in `models/aggregates.py` rather than beside the domain.
  Keyed on `(station, train, dow)`, where `dow` is `0`=Monday..`6`=Sunday, taken from
  `trip.service_date` (the departure date at the run's *origin*, so an overnight run keeps
  one weekday end to end). A weekday and a Sunday are different railways; one mean over
  both describes neither. The cost is that samples divide by seven, so a single week gives
  one run per row — these only start to mean "usually" after several weeks.

  Not keyed on `service_line`: `upsert_line` only mints a line when both category and line
  are present, and no long-distance stop carries `dp@l` (§11), so every ICE and IC would
  be dropped.
- **`train_stop_average`** — the same idea keyed differently: `(day_type, train, station,
  scheduled time)`, where `day_type` is `WEEKDAY` (Mon–Sat, excluding public holidays) or
  `SUNDAY_HOLIDAY`. Pooling the working days rather than splitting them by weekday means a
  figure means "usually" after weeks rather than months; keying on the scheduled time
  keeps a train that runs a different timetable on Saturday from being averaged against
  itself. Read through the `avg_weekday` / `avg_sunday_holiday` views.
- **`tt_message`** — delay causes attached to a stop. The most useful label in the
  database for training.

Recording how it was obtained: `tt_poll` (which station we read, when — the listening
record), `tt_plan_fetch` (which schedule buckets are already captured), `tt_unmatched`
(events we hold no schedule for). Provenance: `feed_version`, `route`, `search_alias`.

### The seven day views

`trip_stop` carries no date of its own — it is reached through `trip_id`, and
`trip.service_date` is what says which day a run belongs to. That join is written once, in
`src/dbrel/views.py`, and given seven names:

```sql
SELECT * FROM day_monday WHERE station_id = 42;   -- ... through day_sunday
```

Each view is every stop of every run that departed its origin on that weekday, carrying
`service_date` so one Monday is still distinguishable from another. They are created by
`create_all()`, so they exist from the first `init-db`.

One trap they exist to hide: SQLite's `strftime('%w')` is **0=Sunday**, while Python's
`date.weekday()` — which is what the `dow` column stores — is **0=Monday**. The views
convert deliberately, and `tests/test_views.py` pins the two against real dates.

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
WHERE d.station_id = ? AND d.n_dep >= 5
ORDER BY d.avg_dep_delay_s DESC LIMIT 20;
```

The first query is what `compute-averages` precomputes into `train_station_delay`, one row
per train per station, so the web board does not recompute it per request.

`show-train` and `show-station` both take `--format tsv` for piping straight into a
training script.

## Deploying (home server)

The home server runs the image from the `Dockerfile` (Django API, the built frontend and the
served model from `models/v2/`) with `deploy/compose.yaml`, next to a Cloudflare Tunnel container
that carries `bahnpredict.de`. The database is a file at `/srv/bahnpredict/data/dbrel.sqlite`,
mounted at `/data`. The 48 GB collection
database stays on the collector machine. The server reads a deployed copy (`dbrel.deployed`)
holding:
- the trains, stations, lines, holidays and search aliases
- the schedule, stored as shared stop patterns: each distinct sequence of stops, platforms and
  relative times is stored once, and each route variant keeps only its pattern, start time and row
  ids. A `station_stop` view gives back every row exactly, ids included, so the server's queries
  are the same on both databases.
- a run summary, one row per train and route variant over all recorded runs (last run date and
  run count), in place of `trip`
- route hashes, route paths and place names stored once each; `train` and `train_variant_run` are
  views over the compact tables, so they too give back the collection database's rows exactly
- for retired trains (not seen for 14 days before the newest data) only the train row: the server
  answers their page with "no longer running" and leaves them out of search, on either database.
  Train columns nothing reads are left empty.
- the model's notice-rate totals

There are no per-run observations in it:

```bash
dbrel ml launch build-slim --memory 4G -- env PYTHONPATH=src .venv/bin/python scripts/build_slim_db.py
# -> data/deploy/slim.sqlite; prints the size, schedule rows, patterns and run-summary rows
```

The build reads every schedule row back through the view and compares it with the collection
database, id included, and checks the notice totals against the model's fingerprint. It writes
`<out>.partial` and replaces the output only if everything matches, so a refused build leaves the
previous file in place. To check that a server on the copy answers as one on the collection
database, run the parity check:

```bash
dbrel ml launch compare-deployed --memory 4G -- env PYTHONPATH=src .venv/bin/python \
    scripts/compare_deployed_db.py --deployed data/deploy/slim.sqlite
```

It covers boards, train routes with and without the stop they were opened from, line and corridor
directions, stations and details, the runs before and after a time, and search. It calls them on
the busiest stations, the trains with the most route variants and a seeded random sample, compares
the JSON exactly and exits 1 on any difference.

Every push to `main` that passes the tests deploys itself: the `deploy-home` job runs on the
self-hosted GitHub runner on the server and does `docker compose up -d --build --wait`. The
database is not part of that. Upload a new copy with:

```bash
scripts/push_slim_db.sh [data/deploy/slim.sqlite]   # checkpoint, upload, verify, swap, restart
```

First setup of the server: Docker, `/srv/bahnpredict/.env` (`DBREL_ALLOWED_HOSTS`,
`DBREL_SECRET_KEY`, `TUNNEL_TOKEN` from a Cloudflare Tunnel whose route points to
`http://app:8080`), the database via the script above, and a self-hosted runner labelled `home`
(repository Settings > Actions > Runners).

The model is committed, so a new model means `dbrel ml promote`, a commit and a push to `main`. A model
that uses notice rates reads `notice_rate_total` from the slim database and refuses to predict if
the totals no longer match its manifest fingerprint, so rebuild the slim copy from the same data
the model was trained on.

## Delay model

The web UI's delay forecast runs in the browser (ONNX Runtime Web) from the artifact that
`models/v2/CURRENT` names; the server only encodes each call's inputs. `dbrel ml
predict-delay --train "RE 10070" --station "Dortmund Hbf" --date D` prints the same forecast
on the command line, and `--artifact models/<dir>/<stamp>` runs any other artifact
without reading `CURRENT`.

**The full-history dataset.** `dbrel ml build-full-dataset` turns every finalized arrival in
the database into one shard per calendar month under `data/ml/arrival_full/`, and prints
each month's row count (`*` marks the months it rebuilt). Reruns rebuild only the latest
month and any month whose rows changed; `--full` rebuilds everything. It only reads the
database.

**Notice rates.** The full-year models also take six historical *notice rates*: the share of
past arrivals whose trip had a construction notice ("Bauarbeiten") or a disruption notice
("Störung", "Großstörung"), for the arrival's station, line and train. `dbrel ml
build-notice-rates` fills the `notice_rate_daily` table from `trip`, `trip_stop` and
`tt_message` (about 20 minutes and 0.3 GiB; it only writes that table, and a date's rows are
replaced, so it can be re-run; `--from D --to D` limits it to those service dates). Run it
capped, before a search: `dbrel ml launch notice-rates --memory 4G -- dbrel ml
build-notice-rates`. It prints notices per 1,000 trips for each month and warns about a month
far below the median. A training row's rates leave out its own calendar month, and validation,
test and live predictions use the training dates only, so a notice caused by a delay never
reaches its own arrival. Prediction reads the stored counts and makes no API call. The
artifact's manifest records the training dates and a fingerprint of their counts; if
those dates' counts in `notice_rate_daily` change later (for example a full rebuild after old
notices were corrected), the model refuses to predict until the rows are restored or it is
retrained. Add new days with `--from <first new date>`.

**The full-year search** (`scripts/search_full_year.py`, PyTorch-ROCm venv) trains two
arrival-only median models on that dataset: a *compact* one (download ≤ 35 MB, may be
served) and a *large* one (≤ 100 MB, comparison only, never served). Split: the latest 7
complete days are a sealed test week, the 28 days before are validation, everything earlier
is training. Every training step runs as a capped job, after a memory check:

```bash
dbrel ml launch notice-rates --memory 4G -- dbrel ml build-notice-rates   # once, and after new days
PY="env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/search_full_year.py"
dbrel ml launch full-year-memcheck-compact --memory 14G --oom-policy continue -- $PY memory-check --track compact
dbrel ml launch full-year-compact --memory 14G -- $PY search --track compact   # resumable
dbrel ml launch full-year-large   --memory 14G -- $PY search --track large     # after its own memory check
dbrel ml launch full-year-final   --memory 14G -- $PY final --tracks compact,large
$PY export --track compact --final-dir data/ml/full_year/final/<last test date>
```

`dbrel ml launch` refuses a `search` or `final` step if the track has no memory check for
the current dataset, or if the projected peak (plus 15%) is over the cap. `search` writes
`data/ml/full_year/runs/<track>-<last test date>/ranking.json`: every trial ranked by
validation average error (MAE), the trials skipped for size, and the reference models (the
old two-sided TabM and the window MLP) scored on the same rows. `final` refits each track's
winner and scores it once on the test week, then records those dates in
`data/ml/inspected_dates.json`, so the next final test needs newer data. `export` writes a
compact model to `models/v2/` and a large one to `models/v2-comparison/`. Neither
moves `CURRENT`. Serving a model is a separate, deliberate step, which refuses comparison
artifacts:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from pathlib import Path; from dbrel.ml.arrival import promote; print(promote(Path('models/v2'), '<stamp>'))"
```

It prints the previous stamp; promoting that stamp again rolls back (only the deployed model is
committed, so a rollback target has to be kept or retrained). The committed model uses notice
rates, so a fresh clone needs `dbrel ml build-notice-rates` on a database holding the same data
before it can predict; serving refuses if the counts differ from the manifest's fingerprint. `dbrel study
live-accuracy --artifact <dir>` scores any artifact against live trains.

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
- **Timetables covers a tracked subset, not all of Germany** — it grows by command rather
  than by migration, since `stop_sequence` is sparse, so adding a station fills a gap
  instead of renumbering any history. The old form of this note put a number on the
  ceiling (~5,400 stations needing ~518k requests/day against an 86k budget at 60/**min**);
  that number no longer holds now that the client runs at 60/**s** by default (§27,
  `src/dbrel/config.py`), so it is not repeated here rather than left stale. Whether the
  full national set is now actually reachable hasn't been tested — see the TODO below,
  which has the same caveat.
- **TODO: whether a one-shot full-day `/plan` backfill can be scaled to all of Germany is
  now an open question again, not a settled "no."** §26 confirmed live on the 212-station
  backfill that major hubs 404'd during morning rush hour because their target hour had
  already aged past `/plan`'s ~14h same-day retention (§24) by the time the backfill
  reached them, not because no train ran, and calculated that a flat national sweep at
  **50 rpm** — the rate assumed at the time — could never capture more than the first 6-7
  hours of any day (math: `(h+1)×2.74h ≤ h+14h` only holds through hour ≈6). §27
  subsequently found the gateway does not actually enforce that figure, and the live
  client now runs at 60/**s**, not 60/min — two orders of magnitude faster than this
  calculation assumed. The conclusion has not been re-derived at the new rate (that needs
  a sustained, not burst, test against the real gateway — §27's caveats), so this remains a
  TODO rather than a claim either way. The same recompute affects the *recurring* cycle too
  (an 8h rolling window across 8,215 stations costs
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
