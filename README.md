# dbrel — German train reliability database

A local SQLite database of German long-distance and regional trains recording, for
every stop of every run, both the **supposed** and the **actual** arrival/departure
times.

Data comes from [gtfs.de](https://gtfs.de) (DELFI-derived, CC BY-SA 4.0). **No API key
is required.**

## Quick start

```bash
uv sync
uv run dbrel init-db
uv run dbrel import-static          # schedules: fv (long distance) + rv (regional)
uv run dbrel poll                   # one realtime snapshot -> observations
uv run dbrel finalize               # observations -> actual times
uv run dbrel stats
```

`poll` is one-shot by design. Run it repeatedly to build up history — each fetch sees
only the trains running in roughly the next hour, so a full day of actuals needs polling
through the day:

```bash
while true; do uv run dbrel poll; sleep 300; done
# or: */5 * * * * cd /path/to/repo && uv run dbrel poll
```

Each fetch downloads ~47 MB uncompressed (gtfs.de offers no gzip), so 5-minute polling
costs roughly 13 GB/day. Choose the interval accordingly.

## Commands

| Command | Does |
|---|---|
| `init-db` | Create the schema |
| `import-static [--feed fv,rv] [--force] [--max-days N]` | Download and import the timetable; idempotent on content hash |
| `poll [--dry-run]` | One realtime fetch; appends only values that changed |
| `finalize [--date YYYY-MM-DD] [--force]` | Derive actual times from observations |
| `stats` | Row counts, poll history, quality breakdown, punctuality |
| `unmatched` | Realtime trips that resolved to no known train |

## The data model

Five core tables, six supporting.

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

- **`station`** — a station. Keyed on normalised name + rounded coordinates, because
  gtfs.de stop ids are opaque integers with no documented stability.
- **`train`** — a recurring service. Keyed on product, operator, departure time and its
  exact sequence of stations, so it survives the weekly feed rebuild.
- **`station_stop`** — one stop in a train's planned pattern. Times are seconds from the
  service-date anchor, which is what makes `25:30:00` representable.
- **`trip`** — one dated run: links a train to a date, carries journey-level timing.
- **`trip_stop`** — the fact table: supposed vs actual, per stop, with a quality grade.

Supporting: `feed_version` (provenance), `route`, `trip_service_date`, `rt_poll`
(the listening record), `rt_observation` (raw predictions), `rt_unmatched`.

`trip` rows exist for every scheduled run. `trip_stop` rows are materialised only for
runs actually observed — the full timetable would be ~1.5M stops per day.

## What "actual" means here

**The realtime feed publishes predictions, not verified actuals.** No source in this
project ever states that a train departed. An actual time is therefore derived:

> the last published estimate for an event, observed after the event should have
> happened, that then stopped changing.

Every `trip_stop` records the rule that produced it (`rule_version`) and how much
confidence it deserves (`quality`):

| grade | meaning |
|---|---|
| `OBSERVED` | Revised after the event, then held while we were still polling. Strongest. |
| `INFERRED_DOWNSTREAM` | The run was still reporting about later stops, so this value stood. |
| `LAST_SEEN` | The run left the feed before settling; its last word is carried. |
| `PREDICTION_ONLY` | No poll covered the moment it mattered. `poll_gap_s` says how wide the blind spot was. |
| `SKIPPED` | The feed said this stop was skipped. |
| `NO_DATA` | No realtime information. **Never means "on time".** |
| `MISSING` | Scheduled, ran while we were polling, never appeared. |

Runs whose window no poll covered are **left unjudged** rather than marked missing —
"the train did not run" and "we were not looking" are different claims, and only the
first is a fact about the railway.

Because `rt_observation` keeps the raw prediction stream, a corrected rule can be
re-run over it (`finalize --force`) and compared against the old output.

## Analysing it

Plain SQL over `trip_stop`. Filter on quality — untrusted grades will skew any average:

```sql
-- punctuality by station (DB counts under 6 minutes as on time)
SELECT s.name, COUNT(*) n, AVG(ts.dep_delay_s)/60.0 mean_min,
       1.0*SUM(ts.dep_delay_s < 360)/COUNT(*) pct_on_time
FROM trip_stop ts JOIN station s ON s.id = ts.station_id
WHERE ts.quality IN ('OBSERVED','INFERRED_DOWNSTREAM') AND ts.dep_delay_s IS NOT NULL
GROUP BY s.id HAVING n >= 20 ORDER BY mean_min DESC;

-- how one train has behaved over time
SELECT t.service_date, t.final_delay_s/60.0 min_late, t.status
FROM trip t WHERE t.train_id = ? ORDER BY t.service_date;
```

## Probes

`scripts/probe_match.py` and `scripts/probe_validate.py` measure the feeds rather than
trusting them: id-space agreement, realtime coverage, update semantics. Findings are
written up in [docs/probe-findings.md](docs/probe-findings.md) — worth reading before
changing the ingest, and worth re-running if the feeds change shape.

## Notes and limits

- **The realtime feed is a rolling window** of trips running now or starting within
  ~1 hour, not a whole day. A single poll can never cover a full service day.
- **Coverage is ~86%** of in-progress rail trips (fv 66%, rv 89%). Long-distance scores
  lower because overnight services carry no realtime data.
- **There is no train number** in these feeds — `trips.txt` carries only `route_id`,
  `service_id`, `trip_id`. "ICE 78" in `train_name` is built from the product plus
  origin and destination, not from an official number.
- **Trip-level cancellations are not signalled.** Every descriptor arrives as
  `SCHEDULED`; cancellation is only visible per-stop via `SKIPPED`.
- Static feeds cover the next 30 days and are rebuilt roughly weekly.
