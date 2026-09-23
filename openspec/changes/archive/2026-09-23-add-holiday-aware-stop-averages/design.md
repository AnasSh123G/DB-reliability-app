# Design

## Context

- `trip_stop` (fact table) has `arr_delay_s`/`dep_delay_s`, `quality`, `station_id`, and
  `supposed_arrival_utc`/`supposed_departure_utc` as absolute epoch, but no direction and no
  "scheduled time of day" field — those must be derived.
- Direction is not a stored column. `web/queries.py::_direction_of(train, index)` already
  derives it (0/1) from a train's position along its line's longest `route_path`, because
  short workings (e.g. RB33) terminate mid-line and origin/destination pairs alone
  under-count directions. Reuse this, don't reimplement it.
- Per `[[dbrel-aggregates]]` memory: trip-level origin time (`trip.supposed_departure_utc`) is
  unstable — it's the first *tracked* stop, which moves when a backfill hour is missing.
  Scheduled time *at the station being averaged* is stable and is the right averaging key,
  confirmed by the project's prior (deleted) `FEATURE_SPEC` design.
- `public_holiday` is currently `date` (primary key) -> `name`, one flat nationwide list,
  populated only with NRW's 11 hand-rolled holidays (`holidays_de.py`). National tracking
  (memory `[[dbrel-national-collection]]`) covers all 16 states, so this is now wrong for the
  ~96% of stations outside NRW.
- `Station` has `lat`/`lon` but no state. No existing geocoding or boundary-lookup dependency
  in the project (`pyproject.toml` core deps: sqlalchemy, typer, httpx, jinja2-family; `pyarrow`
  is an optional extra for archive reading only).
- This reverses the 2026-09-21 "live inference only" decision — confirmed directly with Anas
  (see proposal.md). It does not touch or depend on the deleted ML pipeline.

## Goals / Non-Goals

**Goals:**
- Resolve every tracked station to one of the 16 German Bundesländer, once.
- Replace the NRW-only holiday ruleset with all-16-states coverage, keyed by state.
- Build a materialized average table keyed by `(station, line, direction, scheduled
  time-of-day, holiday_flag)` from `RECORDED`-quality `trip_stop` rows.
- Keep the three implementation stages cheaply separable, per Anas's request, so the two
  fetch-only stages can run on a smaller/cheaper model and only the schema+aggregation stage
  needs a stronger one.

**Non-Goals:**
- Live-scored prediction, or anything that touches the deleted ML pipeline — that redesign is
  separate and still requires asking Anas directly before starting (`[[dbrel-ml-pipeline]]`).
- Real-time/incremental average updates. The table is rebuilt on demand via a CLI command, not
  kept continuously in sync with the poller.
- Averaging over stations with unresolved state (see holiday-calendar spec's scenario on
  out-of-bounds coordinates) — these are excluded, not force-assigned.

## Decisions

### Execution split: three roles, not three permanent runtime agents
The three "subagents" Anas described (backfill-fetcher / holiday-fetcher / database-organizer)
are an **execution strategy for implementing this change**, not a new piece of product
architecture. `dbrel` does not gain a runtime agent system. Concretely, at apply time:
- **backfill-fetcher (Haiku)**: runs the *existing, already-tested* `dbrel backfill` command
  (archive-fetch + replay) to ensure `trip_stop` is populated. No new code — it operates the
  pipeline built in `add-archive-backfill`. Cheap because it's "run a command, watch for the
  exit code," not judgment.
- **holiday-fetcher (Haiku)**: fetches public holiday dates for all 16 Bundesländer for the
  years the tracked data spans, and writes them to an intermediate file (JSON: state -> list of
  {date, name}). Pure I/O, no schema/business logic, so no need for a stronger model.
- **database-organizer (Sonnet)**: does everything that requires judgment — extends the schema
  (`Station.state`, `public_holiday` composite key), resolves station state from coordinates,
  loads the holiday-fetcher's output into `public_holiday`, and writes/runs the aggregation
  that builds the average table.

This keeps the task breakdown (tasks.md) assignable by role without inventing new persistent
infrastructure.

### Holiday source: external per-state API, not a hand-rolled ruleset
`holidays_de.py`'s docstring explains NRW was hand-rolled specifically *because* it was only
one state (11 fixed rules). German holiday law is not that simple at 16-state scope — several
holidays (Fronleichnam, Reformationstag, Allerheiligen, Mariä Himmelfahrt) are Bundesland-
specific, and a few are further restricted to Catholic-majority municipalities within a state.
Hand-rolling 16 rulesets risks silently getting the municipality-level exceptions wrong.
**Decision: fetch from a public holiday API that already models Germany at the
state/subdivision level** (e.g. OpenHolidaysAPI, which explicitly supports German
`subdivisionCode`) rather than re-deriving the rules. `holidays_de.py`'s existing Easter
computation (`easter_sunday`) is kept and reused only as a local fallback/offline check, not as
the source of truth.
- Alternative considered: extend the existing hand-rolled fixed/Easter-relative tables to all
  16 states. Rejected — correctness risk at municipality-level exceptions, and the module's own
  docstring already flags this as the reason it stayed NRW-only.

### Station state resolution: offline point-in-polygon, hand-rolled, no new dependency
**Decision: bundle a German 16-state boundary dataset (GeoJSON,
`resources/de_states.geojson`) and do point-in-polygon lookup locally**, once per station,
with a hand-rolled ray-casting implementation (`geo_states.py`) instead of a geometry library
like `shapely`. Same reasoning as `holidays_de.py`'s NRW ruleset used to be hand-rolled and the
same reasoning `config.py`'s `.env` parser gives for not taking python-dotenv: the project keeps
its dependency list short on purpose, and this is a genuinely small amount of code —
ray-casting (even-odd rule) over Polygon/MultiPolygon rings, with holes handled by counting all
ring crossings (a hole flips parity an extra time) and a per-state bounding-box prefilter so the
common case (most states are checked and rejected instantly) doesn't walk every ring. Sixteen
states' worth of German administrative boundaries is closed, static, well-behaved geometry — no
self-intersections, no need for a general-purpose geometry engine's robustness guarantees — so
the trade shapely would buy (correctness for degenerate polygons, performance on very large
ones) isn't needed here. Alternative considered: call a reverse-geocoding API (e.g. Nominatim)
per station. Rejected as the default — 7,258 stations against Nominatim's 1 req/s usage policy
is a ~2-hour one-time job with an external-service dependency for a lookup that a bundled
boundary file answers deterministically and offline. The boundary file becomes a one-time static
asset, not a runtime dependency.
- **Risk**: coastal/border stations may sit exactly on a boundary or just outside all polygons
  (ferries, cross-border stops). → Mitigation: unresolved stations are recorded as
  `state = NULL` and excluded from holiday-flagged averaging (per spec scenario), not guessed.
  Confirmed against the real database: 33 of 5,422 stations (0.6%) resolve to NULL, and by name
  they are exactly this kind of case — lakeside/border towns on the Bodensee and the French and
  Swiss borders (Konstanz, Laufenburg(Baden), Kleinblittersdorf, Lindau-Insel, ...).

### Averaging key: scheduled time-of-day at the station, PLUS the trip's own start time
Per the `[[dbrel-aggregates]]` memory, `trip.supposed_departure_utc` is unstable (moves when a
backfill hour is missing), but the scheduled time *at the specific station being averaged* is
stable. **Decision: key on `station_id, line_id, direction, trip_start_time,
time_of_day(supposed_departure_utc at that station)`.**

The original version of this decision keyed on station-local scheduled time alone, reasoning
that "a service that starts at 11:00 and one that starts at 15:00 reach any shared downstream
station at two different times of day, so keying on the station-local time already separates
them." That reasoning is wrong when two *different* services happen to reach the same station
at the *same* clock time -- confirmed on the real database: RE7 runs coupled portions
(11159 Flensburg->Hamburg-Altona and 11259 Kiel->Hamburg-Altona) that depart different origins
at different times but are timed to both call at Neumünster at 07:31 before travelling onward
coupled. Station-local scheduled time alone pooled these into one row; a passenger would never
consider them "the same train." **`trip_start_time` -- the run's own planned origin departure,
Berlin-local `HH:MM`, derived per TRIP -- now joins the key**, so these stay separate rows.

Derivation, per trip: for a Timetables-sourced trip, `trip.source_run_key` is
`f"{daily_trip_id}-{start_stamp}"` where `start_stamp` is a literal `yyMMddHHmm` Berlin wall-
clock timestamp of the run's origin departure (see ingest/runs.py, ingest/parser.py's
`split_stop_id`/`parse_ts`) -- always the trailing 10 characters, since `daily_trip_id` is a
signed integer and contains no other hyphen. For a GTFS-sourced trip (none currently reach
`RECORDED` quality, but the fallback exists in case that changes), `train.first_departure_sec`
(the pattern's static origin departure) is used instead, since GTFS-de trips are pattern- not
per-trip-timed. If neither yields a usable value the trip is excluded from averaging and the
count reported, rather than silently guessed. `train.first_departure_sec` alone (the original
plan's suggestion for an *informational* `trip_start_time`) cannot do this job even as a
fallback-of-first-resort: it is per TRAIN (pattern), not per TRIP, and the same train number can
run at a different time on different days.
- Scheduled time-of-day is derived by converting the epoch to Berlin local time and keeping
  `HH:MM` (not `HH:MM:SS` -- IRIS/GTFS scheduled times carry no seconds precision worth keying
  on, and not the epoch itself), so the same daily service on different calendar dates maps to
  the same key.

### Direction: reuse `_direction_of`, but scoped to a connected regional component
Reuse `web/queries.py::_direction_of` (0/1 by position along a route's longest `route_path`)
rather than writing a second direction heuristic -- unchanged from the original decision. Two
implementations of "which way is this train going" would drift, per the same reasoning that kept
`replay.py` from reimplementing `import_plan`'s grading rules.

What changed: a `service_line` id is not one physical line. Short names like "RE7" or "S1" are
reused by unrelated regional services all over Germany with no disambiguation upstream --
confirmed on the real database, where `service_line` id 97 ("RE7") covers nine unrelated
corridors (Karlsruhe<->Basel, Hamburg<->Kiel/Flensburg, Berlin<->Dessau, the Allgäu, ...).
Picking one nationwide "longest route" axis for the whole line and measuring every train's
position against it silently misclassified every train outside whichever single region happened
to have the longest `route_path` -- line 97 split 416/62 instead of splitting per region roughly
50/50. **Decision: before computing a canonical axis, partition a line's trains into connected
components** (`dbrel/direction.py::_connected_components`) via union-find, where two trains are
connected if their `route_path`s share at least 2 station names (1 is too weak -- unrelated
lines often share exactly one interchange hub without being the same corridor; 2 rules that out
while still connecting a Y-branch's two arms, which share a multi-station trunk). Canonical order
and direction are then computed *within* each component, not once for the whole line.
`canonical_order`/`direction_of` themselves are unchanged; `train_direction_map` is the new
entry point that does the partitioning first.
- A Y-shaped line (RE7 Hamburg-Altona<->Neumünster<->{Kiel|Flensburg}) is one component (the two
  branches share the Hamburg-Altona...Neumünster trunk), and its direction split still comes out
  correct: both away-from-Hamburg branches get one direction value, both toward-Hamburg branches
  get the other -- verified against the real database and in `tests/test_direction_components.py`.
- Cosmetic gap, not a correctness one: `line_trains_by_direction`'s `canonical` return value
  (used only for the web UI's "towards X" label and station ordering) is still the single longest
  `route_path` across the whole line, not per-component -- a per-component label would need the
  API to return one label per component, which the current single-label API has no room for. The
  direction VALUE every train gets is unaffected, since that now comes from
  `train_direction_map`, computed per component.

### Schema change to `public_holiday`: composite key, additive migration
`public_holiday.date` (primary key) becomes `(date, state)`. Since Alembic is vestigial here
(per `[[dbrel-aggregates]]` memory — schema changes go through `create_all()` on a rebuilt
database, not a migration), this is a `create_all()`-driven change: the table is dropped and
rebuilt with the new key, then repopulated from holiday-fetcher's output. The 11 existing
NRW-only rows are superseded, not preserved, since they're wrong for every non-NRW station
regardless.

### Plausibility cap on raw delay magnitude: 2 hours, per side, masks rather than fixes
A data-quality audit of the finished 30-day backfill found 1,970 trips (of 1,265,482) with a
`RECORDED`, non-cancelled stop whose `arr_delay_s` or `dep_delay_s` exceeds 2 hours in
magnitude — some exactly -86400s (-24.0h), a day-boundary calculation bug, not a real delay.
993 of those 1,970 trips concentrate on two rail-replacement bus lines (`service_line` 158 "S9"
and 159 "S46"), most likely because their actual times get matched against the original train
line's schedule rather than their own. Anas: "use a threshold of 2 hours... anything delayed by
more than 2 hours should be ignored." **Decision: `stop_averages.py` excludes a stop's
`arr_delay_s`/`dep_delay_s` from `n_arr`/`mean_arr_delay_s` or `n_dep`/`mean_dep_delay_s`
respectively when `ABS(value) > 7200`, independently per side** (`MAX_PLAUSIBLE_DELAY_S`).
This masks the S9/S46 root cause (schedule mismatch) rather than fixing it — the underlying
mismatch, if it also produces plausible-looking but still-wrong delay values, would pass
through uncaught. **Accepted as final, not deferred for later work**: Anas reviewed the
post-threshold numbers (S9 dropped from +74min to +5.5min arrival average, S46 from +35.6min to
+3.5min) and decided the threshold alone is sufficient — the deliverable is a general average,
not per-line accuracy for replacement-bus services, so the remaining small skew on these two
lines is acceptable as-is. Do not re-investigate the S9/S46 schedule-matching root cause or
propose a median/robust-statistic alternative unless Anas raises it again himself.

## Risks / Trade-offs

- [Risk] External holiday API becomes unavailable or rate-limits a 16-state x multi-year fetch
  → Mitigation: holiday-fetcher writes its output to a checked-in intermediate file once
  fetched, so re-running database-organizer doesn't require re-fetching.
- [Risk] Point-in-polygon boundary data may misclassify a small number of edge-case stations
  (enclaves, exact border crossings) → Mitigation: spec requires unresolved/ambiguous stations
  to be excluded rather than force-assigned; this is a visible gap (`state IS NULL`), not a
  silent wrong answer.
- [Risk] Rebuilding the whole average table is a full scan of `trip_stop` (national scale, tens
  of millions of rows once 30-day backfill + ongoing collection accumulate) → Mitigation: this
  is an explicit on-demand CLI command, not something triggered per request or per poll cycle;
  cost is paid once per rebuild, same trade-off already accepted for `dbrel backfill` itself.
- [Trade-off] Averages go stale between rebuilds (same shape of trade-off Anas already accepted
  when rejecting a materialized `TrainStopPrediction` table for the ML design — but here it's
  intentional and requested, not something to re-relitigate).

## Migration Plan

1. Extend `Station` with `state` (nullable) and `PublicHoliday` with the composite
   `(date, state)` key in `models.py`.
2. Rebuild the local dev database via `create_all()` (no data preserved for these two tables;
   `trip_stop`/`trip` data is unaffected since neither table is touched).
3. Run backfill-fetcher, holiday-fetcher, then database-organizer's station-state resolution
   and holiday load.
4. Run the new average-table build CLI command.
5. Rollback: both new tables/columns are additive to the overall schema (nothing else
   references `Station.state` or reads the new average table yet), so rollback is dropping the
   new table and column and reverting `models.py` — no downstream consumer to coordinate with.
