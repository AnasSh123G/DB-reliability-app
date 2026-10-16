# Design

This document is written for an implementer who has not seen the codebase before. Every
constant, name, shape and step is spelled out. When something here and `tasks.md` disagree,
this document wins; report the disagreement.

## Context

See `proposal.md` → "What is missing today" for the gaps. The facts below shape the design.

- **Schedule storage.** `station_stop` holds one row per stop per route variant, keyed by
  (`feed_version_id`, `train_id`, `path_sha1`, `stop_sequence`). `planned_arrival_sec` and
  `planned_departure_sec` are seconds from the service date's anchor and can exceed 86400. Use
  `dbrel.ingest.timeutil.service_date_anchor(d)`, which is local noon minus 12 h and safe across
  DST. A run's absolute time is `service_date_anchor(service_date) + sec`. Only the IRIS feed is
  routed. Its id comes from `dbrel.web.queries.tt_feed_version_id(session)`, which is read-only.
  Never use `runs.tt_feed_version`, which inserts a row.
- **The deployed file.** `data/deploy/slim.sqlite` is 184 MB. `station_stop`, `train` and
  `train_variant_run` are **views** in it. Selecting `id` from `station_stop` runs a subquery per
  row, so never select it in bulk. A `LEFT JOIN` to `train` makes SQLite rebuild the whole
  view; read `train` only with plain `SELECT … FROM train` or `WHERE id = ?`. The file has
  2,943,814 IRIS stop rows, 362,869 IRIS route variants, 9,699 stations and 62,591 running
  trains. It has no `trip` table.
- **The collection database** (`data/dbrel.sqlite`, 51 GB) has `station_stop` and `trip` as real
  tables. The server tells the two apart by whether a `train_variant_run` table exists
  (`queries._variant_runs`). This change does the same for the new profile table.
- **The model.** `models/v2/CURRENT` → `20261014T114610Z`, kind `arrival-net`. Its ONNX output is
  p25/p50/p75 in whole minutes, from -30 to 119. In Python, `dbrel.ml.predict.DelayModel` loads
  it and `DelayModel.predict_calls(session, calls, direction=...)` forecasts a batch of
  `dbrel.ml.timetable.ScheduledCall` (about 8 µs per row for ONNX itself). Building the inputs
  costs more: `direction_by_train_id` is computed per line, and the notice rates load once per
  process in about 25 s (`queries.warm_model`).
- **Browser parity.** The browser gets inputs from `queries._model_input`, which calls
  `calls_at(session, train, station_id, service_date, variant=path_sha1)`. That builds
  `ScheduledCall(stop_rank = 0-based index among the variant's rows ordered by stop_sequence,
  n_stops = number of the variant's rows, trip_start_sec = _variant_trip_start(train, rows))`.
  The planner must build identical `ScheduledCall`s, which makes its forecasts identical.
- **Runtime.** Fly runs a shared-cpu-1x machine with 1 GB RAM. `dbrel serve` runs Django and
  proxies everything that is not `/api/*` to the Node frontend on port 8901. A background thread
  in `src/dbrel/web/wsgi.py` warms the model.
- **Tests.** pytest under `tests/` (`uv run pytest -q`). `tests/conftest.py` points the web layer
  at an empty models directory, so there is no model in tests. The frontend has no tests; it is
  checked with `npx tsc --noEmit`, `npm run lint`, `npm run build` and headless Chromium.

## Goals / Non-Goals

**Goals:**
- A planner whose ranking and change feasibility come from the current model's forecasts.
- Correct on both databases, with identical answers.
- Fits a 1 GB machine and answers a typical plan in a few seconds once warm.
- Every piece of logic can be tested without a model and without a large database.

**Non-Goals:**
- Walking links between nearby stations, platform-specific change times, buses and trams,
  fares, live delays, cancellations, and forecasts of departure delay.
- Fixing the stale-variant bug on boards and train pages. The planner avoids it through the
  running profile, and other views are untouched.
- Pareto-optimal (time × changes) results. The planner ranks by forecast arrival only, with
  changes as a tie-breaker.
- Precomputation (CSA/RAPTOR transfer patterns). Plain A\* is what was asked for.

## Decisions

### D1. Search and forecast on the server

The search touches thousands of runs. The browser cannot hold the national timetable, and
sending it every candidate's model input would mean megabytes per search. The planner therefore
runs inside the Django process. It loads the ONNX artifact with `DelayModel` (onnxruntime is
already a dependency), the only server-side inference in the web app (delay-model spec, new
requirement). The comment above `queries._model_cache` that says the web layer "never itself
loads onnxruntime" must be updated to name this exception.

*Alternative rejected:* search on scheduled times and forecast only the results in the browser.
The forecast would then never decide which connection is feasible, which is the point of the
feature.

### D2. Module layout

New package `src/dbrel/planner/`:

| File | Contents |
|---|---|
| `__init__.py` | empty docstring module |
| `constants.py` | every constant in D11 |
| `profile.py` | running profile: SQL, loader, `day_key`, variant choice (D3) |
| `timetable.py` | `PlannerTimetable`, `load_timetable`, `get_timetable` (D4, D5) |
| `forecast.py` | `ArrivalForecaster` protocol, `ModelForecaster`, `FixedForecaster`, LRU cache (D6) |
| `search.py` | `Ban`, `LegRef`, `SearchResult`, `Budget`, `earliest_arrival` (D7, D8) |
| `routes.py` | route keys, `plan_routes` (K distinct), `route_instances`, `tighten` (D9, D10) |
| `respond.py` | turns legs into the JSON in D12 (needs a session for train names) |

`dbrel.web.queries` gets two thin functions, `plan(...)` and `plan_timings(...)`. They call into
the package and return dicts. The package must not import `dbrel.web.queries` at module import
time, which would be circular. Where it needs a helper from there, it imports inside the
function body.

### D3. Running profile

**Stored form (deployed DB).** New table, declared in `src/dbrel/deployed.py` next to
`VARIANT_RUN_DATA`:

```
variant_weekday_data(train_id INTEGER, path_id INTEGER, weekday INTEGER, n_runs INTEGER,
                     PRIMARY KEY (train_id, path_id, weekday)) WITHOUT ROWID
running_profile_window(first_date TEXT NOT NULL, last_date TEXT NOT NULL)   -- exactly one row
CREATE VIEW train_variant_weekday AS
SELECT d.train_id AS train_id, vp.path_sha1 AS path_sha1, d.weekday AS weekday, d.n_runs AS n_runs
FROM variant_weekday_data d JOIN variant_path vp ON vp.id = d.path_id
```

`weekday` is 0 = Monday … 6 = Sunday. `path_id` 0 means a NULL `path_sha1`, as in
`variant_run_data`.

**One SQL for both databases** (`profile.PROFILE_SQL`, a format string). The deployed build
runs it with `trip='src.trip'`, `holiday='src.public_holiday'`, `train='src.train'`. The planner
on the collection database runs it with plain names.

```sql
SELECT t.train_id, t.path_sha1, (CAST(strftime('%w', t.service_date) AS INTEGER) + 6) % 7 AS weekday,
       COUNT(*) AS n_runs
FROM {trip} t
WHERE t.service_date BETWEEN :first AND :last
  AND t.service_date NOT IN (SELECT h.date FROM {holiday} h WHERE h.partial = 0)
  AND t.train_id IN (SELECT r.id FROM {train} r WHERE r.last_seen_date IS NULL OR r.last_seen_date >= :cutoff)
GROUP BY t.train_id, t.path_sha1, weekday
ORDER BY t.train_id, t.path_sha1, weekday
```

- `:last` = `SELECT MAX(service_date) FROM {trip}`.
- `:first` = `:last` minus 27 days, both as `YYYY-MM-DD` (constant `PROFILE_DAYS = 28`).
- `:cutoff` = `MAX(train.last_seen_date)` minus `ACTIVE_WINDOW_DAYS` (14), the same rule as
  `queries._active_cutoff` and the deployed build's `retired` table.

Check the `public_holiday` column names in `src/dbrel/models.py` (`PublicHoliday`). The partial
flag column is `partial`; if it is named otherwise, use the real name.

**Loader.** `profile.load_run_profile(session) -> RunProfile`, with

```python
@dataclass(frozen=True)
class RunProfile:
    counts: dict[tuple[int, str | None], tuple[int, int, int, int, int, int, int]]  # (train_id, path_sha1) -> runs per weekday
    first_date: date | None
    last_date: date | None
```

- If `sqlite_master` has a view or table named `train_variant_weekday`, it reads that view and
  `running_profile_window`.
- Otherwise, if a `trip` table exists, it runs `PROFILE_SQL` with plain names.
- Otherwise it returns an empty profile.

**Day key.** `profile.day_key(service_date, state, holiday_set) -> int` returns 6 when
`dbrel.holidays_de.is_holiday(service_date, state, holiday_set)` is true (that includes every
Sunday), else `service_date.weekday()`. `holiday_set` comes from
`dbrel.holidays_de.load_holiday_dates(session)`, loaded once per timetable build. `state` is
the **boarding** station's state.

**Variant choice.** `profile.chosen_variants(profile) -> dict[int, list[str | None | NOTHING]]`
maps a train id to a list of 7 entries. Entry `k` is the chosen `path_sha1` for day key `k`, or
the sentinel `NOTHING` (a module-level `object()`) when no variant qualifies. A variant
qualifies for `k` when `counts[k] >= PROFILE_MIN_RUNS` (2). Among the qualifying variants, take
the maximum by `(counts[k], sum(counts), path_sha1 or "")`. The last element only makes the
choice deterministic.

### D4. In-memory timetable index

`timetable.PlannerTimetable` is a plain class holding numpy arrays. It is built once per
process and per database (`get_timetable(session)`, guarded by a `threading.Lock`, cached in a
module dict keyed by `str(session.get_bind().url)`). After the database file is rebuilt, the
server must be restarted. This is the same rule as for the notice rates.

Stations are referred to internally by a **compact index** `si` (0…S-1), never by id, so that
numpy can index by station.

| Array | dtype | Length | Meaning |
|---|---|---|---|
| `station_id` | int64 | S | id of station `si` |
| `station_lat`, `station_lon` | float64 | S | degrees |
| `station_state` | list[str or None] | S | ISO state, for `day_key` |
| `station_name` | list[str] | S | name |
| `si_of_id` | dict[int, int] | | station id → `si` |
| `stop_variant` | int32 | N | variant index `v` of each stop row |
| `stop_si` | int32 | N | station of each stop row |
| `stop_seq` | int32 | N | `stop_sequence` |
| `stop_arr`, `stop_dep` | int32 | N | service-day seconds, `NO_TIME` (= -2147483648) for NULL |
| `var_first` | int32 | V | index of the variant's first row; rows are contiguous and sorted by `stop_seq` |
| `var_len` | int32 | V | number of rows (all rows of that train + path in the IRIS feed) |
| `var_cut` | int32 | V | positions `>= var_cut` are unusable (D5) |
| `var_train` | int64 | V | train id |
| `var_path` | list[str or None] | V | `path_sha1` |
| `var_days` | uint8 | V | bit `k` set ⇔ this variant is the chosen one for its train on day key `k` |
| `var_line_key` | list[str] | V | line key of its train (D9) |
| `dep_rows` | int32 | D | stop rows with `stop_dep != NO_TIME` and position `< var_cut-1`, sorted by `(stop_si, stop_dep)` |
| `dep_time` | int32 | D | `stop_dep[dep_rows]` |
| `dep_start` | int64 | S+1 | rows of station `si` are `dep_rows[dep_start[si]:dep_start[si+1]]` |
| `first_date`, `last_date` | date or None | | from the profile, for `data_until` |
| `holiday_set` | set | | `load_holiday_dates` result |

**Build (`load_timetable(session)`)**, in this order:

1. `fv = queries.tt_feed_version_id(session)`, imported inside the function. If it is None,
   return an empty timetable (S stations, N = V = 0).
2. `profile = load_run_profile(session)`, `chosen = chosen_variants(profile)`, and
   `holiday_set = load_holiday_dates(session)`.
3. Stations: `SELECT id, name, lat, lon, state FROM station ORDER BY id`. Every station gets an
   `si`.
4. Stops: run
   `SELECT train_id, path_sha1, stop_sequence, station_id, planned_arrival_sec, planned_departure_sec FROM station_stop WHERE feed_version_id = :fv`
   through `session.connection().exec_driver_sql` (or `session.execute(text(...))`) and read it
   with `fetchmany(100_000)` in a loop. **Do not** add `ORDER BY` and **do not** select `id`.
   Skip every row whose `(train_id, path_sha1)` is not a key of `profile.counts`; these variants
   never run in the window. For each kept row:
   - give `(train_id, path_sha1)` a variant index from a dict, the first time it is seen;
   - append to `array.array('i')` buffers (variant, si, seq, arr or NO_TIME, dep or NO_TIME).

   Do not build lists of tuples: 2.9 M tuples would cost about 600 MB.
5. Convert the buffers with `np.frombuffer(buf, dtype=np.int32)` (or `np.array(buf, np.int32)`)
   and sort with `order = np.lexsort((stop_seq, stop_variant))`, then apply `order` to every
   stop array. `var_len = np.bincount(stop_variant, minlength=V)` and
   `var_first = np.concatenate(([0], np.cumsum(var_len)[:-1]))`.
6. `var_cut` (D5). This is a Python loop over V variants and is fine (about 3 M steps).
7. `var_days`: for variant `v` with train `tr` and path `p`, set bit `k` when
   `chosen[tr][k] == p` (compare with `==`; `None == None` is true for the NULL path).
8. `var_line_key` (D9).
9. Departure index: `mask = (stop_dep != NO_TIME) & (position < var_cut[stop_variant] - 1)`,
   where `position = arange(N) - var_first[stop_variant]`. Then
   `rows = np.nonzero(mask)[0]`, `rows = rows[np.lexsort((stop_dep[rows], stop_si[rows]))]`,
   `dep_time = stop_dep[rows]`, and
   `dep_start = np.searchsorted(stop_si[rows], np.arange(S + 1), side="left")`.
10. Log at INFO: N, V, S, D, build seconds, and `tracemalloc`-free estimate =
    sum of `.nbytes` of all arrays.

Also provide `PlannerTimetable.from_columns(...)`, which does steps 5–9 from given arrays, so
that tests can build a timetable from a handful of rows without a database. `load_timetable`
must call `from_columns` after step 4, so there is only one code path.

**Departures (`tt.departures(si, lo_epoch, hi_epoch) -> list[Departure]`).**

```python
@dataclass(frozen=True)
class Departure:
    variant: int
    service_date: date
    position: int        # 0-based row index within the variant
    dep_epoch: int
```

1. `a, b = dep_start[si], dep_start[si+1]`; `times = dep_time[a:b]` (sorted ascending).
2. For each `service_date` in `dict.fromkeys((local_date(lo_epoch) - 1 day, local_date(lo_epoch),
   local_date(hi_epoch)))`, the same set as `queries.board_slices`:
   - `anchor = service_date_anchor(service_date)`;
   - `i = searchsorted(times, lo_epoch - anchor, "left")`,
     `j = searchsorted(times, hi_epoch - anchor, "right")`;
   - `k = day_key(service_date, station_state[si], holiday_set)`;
   - for each `r` in `dep_rows[a+i : a+j]`: `v = stop_variant[r]`; skip unless
     `(var_days[v] >> k) & 1`; append
     `Departure(v, service_date, r - var_first[v], anchor + stop_dep[r])`.
3. Return the list sorted by `dep_epoch`, then `variant`.

A run is identified by `RunKey = tuple[int, date]` (variant, service_date). A train has at most
one run per service date.

### D5. Faulty schedule steps

For each variant, walk its rows in order with `last = None`:

- `t_in = arr if arr != NO_TIME else dep`
- `t_out = dep if dep != NO_TIME else arr`

A row where both times are `NO_TIME` is skipped and does not move `last`. At the first row `i`
with `last is not None and (t_in < last or t_in - last > MAX_STEP_S)`, set `var_cut = i` and
stop. Otherwise `last = t_out`. If no such row exists, `var_cut = var_len`. Rows at positions
`>= var_cut` are never boarded or alighted at. For the example in the spec, stop 7 at 18180
then stop 8 at 104220 is a jump of 86040 s > 21600, so the cut falls at stop 8.

### D6. Forecasts

```python
class ArrivalForecaster(Protocol):
    stamp: str
    def forecast(self, runs: list[RunKey]) -> dict[RunKey, np.ndarray]:
        """For each run, a float32 array of shape (var_len[v], 3): p25, p50, p75 minutes per row.
        NaN rows where there is no forecast (no scheduled arrival, or the model gave none)."""
```

**`ModelForecaster(tt, session, artifact_dir, stamp)`** is created per request, because it
holds the request's session.

- `self.model`: a `DelayModel(artifact_dir)` taken from a module-level dict keyed by `stamp`.
  Create it once per stamp under a lock, and keep only the newest stamp. Planner ONNX
  sessions use one inference thread; other model consumers retain their default configuration.
- `self.direction`: a module-level planner direction cache per `(tt, stamp)`. Regional lines
  delegate to `_ModelDirectionCache` imported locally from `dbrel.web.queries`. Line-less
  categories use immutable scalar route snapshots read once per category, with nonempty tokens
  cached for membership and raw token count retained for the exact longest-route/id tie rule.
  This computes the same `corridor_direction` values without repeated ORM scans. No ORM
  objects outlive the request session.
- `self.trains`, `self.stations`: dicts filled with `session.get(Train, id)` and
  `session.get(Station, id)`. These are per request; ORM objects must not outlive the session.
- `forecast(runs)`:
  1. Split `runs` into those found in the module LRU (`_FORECAST_CACHE`, an
     `OrderedDict[(tt, stamp, v, service_date)] -> ndarray`, max `FORECAST_CACHE_RUNS` entries,
     guarded by a lock) and the missing ones. `tt` is the timetable object identity, so
     numeric variant indices from different databases or timetable generations never share forecasts.
     Using the object itself avoids identity reuse while an entry remains cached.
  2. For each missing run `(v, d)`, take `train = trains[var_train[v]]`. Build `rows` as
     `[_Row(stop_sequence, planned_arrival_sec, planned_departure_sec)]` for **all**
     `var_len[v]` rows, with NO_TIME turned into None. `_Row` is a NamedTuple with exactly those
     attribute names. Then
     `start = dbrel.ml.timetable._variant_trip_start(train, rows)`, and for each position `p`
     with `planned_arrival_sec is not None` and `p < var_cut[v]`, a
     `ScheduledCall(train, station, d, arr, dep, stop_rank=p, n_stops=var_len[v], trip_start_sec=start)`.
  3. Run one `self.model.predict_calls(session, calls, direction=self.direction.get)` for all
     calls of all missing runs.
  4. Write `pred.arr` (p25, p50, p75), or NaN when None, into each run's array. Store the arrays
     in the LRU and return all requested runs.

**`FixedForecaster(tt, delays, default=(0.0, 0.0, 0.0), stamp="test")`** is for tests. `delays`
maps `(train_id, station_id)` to a `(p25, p50, p75)` tuple, and every row with a scheduled
arrival gets it (or `default`). It also counts calls (`self.calls`) and the runs asked for
(`self.runs_forecast`), so tests can check batching.

**Applying a forecast.** `delay_s = 0 if isnan(p50) else max(0, round(p50 * 60))`;
`arrival_epoch = anchor + stop_arr + delay_s`. A row whose p50 is NaN gives a leg with
`"forecast": null`.

### D7. A\* with lazy run expansion

Edges are rides. Boarding a run at position `p` reaches every later usable stop of that run. A
run's forecasts are needed only when the search actually gets to it. A run's scheduled arrival
is a lower bound on its forecast arrival, because a delay is never below 0. The search can
therefore queue a run under that bound and forecast it only when it reaches the front of the
queue. This lazy expansion keeps the number of forecast runs small.

```python
@dataclass(frozen=True)
class Ban:            # a leg the search may not use
    line_key: str
    board_si: int

@dataclass(frozen=True)
class LegRef:
    variant: int
    service_date: date
    board_pos: int
    alight_pos: int

@dataclass
class Label:
    arrival: int          # forecast arrival epoch (or the requested time at the start)
    legs: int             # trains used so far
    leg: LegRef | None    # how we got here (None at the start)
    prev_state: tuple | None  # immutable predecessor state key
    first_departure: int | None  # scheduled departure of the first train

@dataclass
class SearchResult:
    legs: list[LegRef] | None     # None = not found
    hit_limit: bool
    runs_forecast: int

class Budget:   # shared by every search of one request
    def __init__(self, max_runs_per_search, search_seconds, request_seconds): ...
    def start_search(self): ...   # resets the per-search counters and clock
    def charge(self, n_runs): ...
    def exhausted(self) -> bool: ...   # per-search runs or seconds, or the request's seconds
    def request_exhausted(self) -> bool: ...   # the request's seconds only
```

Heuristic: `h[si] = int(haversine_m(lat[si], lon[si], lat[target], lon[target]) / V_MAX_M_S)`,
computed once per search as a numpy array over all stations. Use the standard haversine with
R = 6,371,000 m.

**Station states.** Key labels by `(si, legs, arrival, first_departure)`, retaining every
 distinct state within the existing budgets. Do not discard a later arrival merely because an
 earlier arrival reached that station: its three-hour transfer window ends earlier. Likewise,
 retain arrivals with fewer used legs, which may be needed under `MAX_LEGS`. The first departure
 preserves the latest-departure tie rule. Each label stores an immutable predecessor state key,
 so reconstruction cannot accidentally follow another arrival at the same station.

Heap items are tuples `(f, legs, -first_departure, seq, kind, payload)`, where `seq = next(counter)` breaks ties
and `kind` is `STATION = 0` or `RUN = 1`. A station payload is its full state key. A run scan
stores the full predecessor state key in addition to its boarding position and run key.

```text
earliest_arrival(tt, fc, source, target, depart_epoch, bans, budget, cache):
    budget.start_search()
    start = (source, 0, depart_epoch, None)
    labels = {start: Label(depart_epoch, 0, None, None, None)}
    heap = [(depart_epoch + h[source], 0, -depart_epoch, seq, STATION, start)]
    settled = set(); boarded = {}; best_target = None
    horizon = depart_epoch + MAX_JOURNEY_S
    while heap:
        if best_target is not None and heap[0].f > best_target.arrival: break
        if budget.exhausted(): return SearchResult(best target legs or None, True, ...)
        f, legs, _, _, kind, payload = heappop(heap)
        if kind == STATION:
            state = payload; si, _, arrival, first_departure = state
            if state in settled: continue
            settled.add(state); lab = labels[state]
            if si == target:
                keep the best target by (arrival, legs, -first_departure)
                continue  # finish all equal-arrival lower bounds before choosing a tie winner
            if lab.legs >= MAX_LEGS: continue
            at_source = lab.legs == 0  # a return to the start station is a change
            if at_source: lo, hi = arrival, arrival + MAX_WAIT_START_S
            else: lo, hi = arrival + MIN_TRANSFER_S - MAX_DEP_DELAY_S, arrival + MAX_WAIT_TRANSFER_S
            for d in tt.departures(si, lo, hi):
                if Ban(tt.var_line_key[d.variant], si) in bans: continue
                lb = lower_bound(tt, d, tt.var_cut[d.variant], h)
                if lb is None: continue
                first_dep = d.dep_epoch if at_source else first_departure
                heappush(heap, (lb, lab.legs + 1, -first_dep, seq, RUN, RunScan(run key, d.position, state)))
        else:
            scan = payload; lab = labels[scan.prev_state]
            forecast scan.key if absent from the shared per-request cache, batching other
                pending RUN keys with f <= current f + FORECAST_BATCH_WINDOW_S,
                at most FORECAST_BATCH_MAX_RUNS; charge budget
            v, d = scan.key; anchor = service_date_anchor(d); base = tt.var_first[v]
            arr = cache[scan.key]
            if lab.legs > 0 and expected_departure(tt, arr, anchor, base, scan.board_pos) < lab.arrival + MIN_TRANSFER_S:
                continue
            first_dep = scheduled boarding departure if lab.legs == 0 else lab.first_departure
            board_key = (scan.key, lab.legs, first_dep)
            prev = boarded.get(board_key)
            if prev is not None and scan.board_pos >= prev: continue
            boarded[board_key] = scan.board_pos
            end = tt.var_cut[v] if prev is None else prev + 1
            for j in range(scan.board_pos + 1, end):
                a = tt.stop_arr[base + j]
                if a == NO_TIME: continue
                t = anchor + a + delay_s(arr[j, 1])
                if t > horizon: continue
                sj = tt.stop_si[base + j]; new_legs = lab.legs + 1
                state = (sj, new_legs, t, first_dep)
                if state in labels: continue
                labels[state] = Label(t, new_legs, LegRef(v, d, scan.board_pos, j), scan.prev_state, first_dep)
                heappush(heap, (t + h[sj], new_legs, -first_dep, seq, STATION, state))
    return SearchResult(best target legs or None, False, ...)
```

- `RunScan` is a frozen dataclass retaining `(key, board_pos, prev_state)`; derived convenience
  fields may be retained. `reconstruct` follows exact predecessor keys, reversing the legs.
- Boarded-run pruning applies only for the same run, used-leg count and first departure.
  Under those conditions an earlier feasible boarding reaches all later rows at identical
  forecast times. Different first departures remain separate to preserve the tie rule.
- Optional dominance optimization: for exactly the same `(si, arrival)` among
  transfer states, fewer used legs dominates more used legs; when leg counts match, a later
  first departure dominates an earlier one. Future windows, bans and forecast arrivals are
  identical, while fewer legs preserves every allowed continuation and wins the final tie.
  Retain immutable predecessor labels and skip superseded station/run expansions. Never
  compare different arrival times. Keep the initial zero-leg source state separate: its
  scheduled boarding window lacks the carried-delay lookback available on a return transfer.
- Cache scheduled lower-bound suffix minima per variant and target, adding each service-date
  anchor afterward. Within one search, index eligible departure rows by station/day key and
  reuse lazy departure records by station/service date. Query the same three service dates,
  inclusive bounds and final ordering as `PlannerTimetable.departures`; do not infer a wider
  horizon. Cache scalar `delay_s` results per run using the existing Python rounding rule.
  Index bans by boarding station. Already-feasible boarded-run claims may be checked before
  queue insertion. Equivalent pending scans with the same run, boarding position, used-leg
  count and first departure retain the earliest feasible predecessor arrival. These
  optimizations do not change feasibility or constants.
- The forecast cache passed into search lives for one request and is shared by all searches.
  The module LRU in D6 sits behind it.
- Batching is required. Forecasting one run per call repeats holiday SQL and feature passes.

Why this is correct: a STATION item's key is exact (`arrival + h`). A RUN item's key is no
greater than the key of every station label it can produce, because each delay is ≥ 0. Keep
all states needed by the waiting and leg limits, and consider all remaining equal-arrival
lower bounds before choosing the target tie winner. The heuristic may be slightly inconsistent
on faulty data (a train faster than 300 km/h straight line). The accepted limitation on such
data remains unchanged. Existing work and time budgets bound the larger state set.

### D8. Change rule and change risk

- **Expected departure of a connecting train.** The model does not forecast departures. A train
  that is late arriving at a station is assumed to leave that station late by the same amount,
  capped at `MAX_DEP_DELAY_S` (1800 seconds),
  and a train that is on time there leaves on time. `forecast.expected_departure(tt, arr, anchor,
  base, pos)` returns `anchor + stop_dep[base+pos] + carried`, where
  `carried = min(MAX_DEP_DELAY_S, delay_s(arr[pos, 1]))` if the row at `pos` has an arrival (`stop_arr != NO_TIME`,
  `pos > 0`) and its forecast is not NaN, else 0. No dwell time is absorbed. This is internal to
  the change rule and is never returned as a departure forecast. At the start station the train
  is boarded at its scheduled departure and this is not used.
- **Feasibility** is checked when a run is taken off the heap (D7): at any station but the start,
  the run is boarded only if `expected_departure >= label.arrival + MIN_TRANSFER_S`. `label.arrival`
  already includes the first train's forecast delay. That is why the departure window of D7 reaches
  back `MAX_DEP_DELAY_S`.
- **Per change** (between leg `i` and `i+1`), with `exp_dep_{i+1}` from `expected_departure`:
  - `planned_min = (sched_dep_{i+1} - sched_arr_i) // 60`
  - `left_min = (exp_dep_{i+1} - expected_arr_i) // 60`
  - `slack_s = exp_dep_{i+1} - MIN_TRANSFER_S - sched_arr_i`, the arrival delay the change can
    absorb.
  - `miss_chance` (`respond.miss_chance`): `null` when p50 is NaN; else `low` if
    `delay_s(p75) <= slack_s`, `medium` if `delay_s(p50) <= slack_s`, `high` if
    `delay_s(p25) <= slack_s`, otherwise `very_high`. These are the bands under 25 %, 25–50 %,
    50–75 % and over 75 %: the model exports only these three quantiles, so nothing finer is
    claimed.
  - `at_risk = miss_chance not in (None, "low")`, which equals the earlier rule
    `sched_arr_i + delay_s(p75) + MIN_TRANSFER_S > exp_dep_{i+1}`.
- The journey's `at_risk` is true when any change is at risk. Its `miss_chance` is the worst
  band among its changes (`respond.worst_miss_chance`, order `low < medium < high <
  very_high`), and `null` without changes or bands.

### D9. Line keys, route keys and three distinct routes

**Line key** of a train, computed for all trains once in `load_timetable`:

1. If the `passenger_line_member` table exists and is not empty, and the train is in it:
   `f"pl{passenger_line_id}"`.
2. Else, if `train.line_id` is not None: `f"sl{line_id}"`.
3. Else: `"c" + "".join(ch for ch in (category or "X") if ch.isalnum())`.

Read 2 and 3 from `SELECT id, line_id, category FROM train` in one query (one full read of
the view is fine once per process), and 1 from `SELECT train_id, passenger_line_id FROM
passenger_line_member`.

**Route key** of a journey: `"~".join(f"{line_key}@{board_station_id}>{alight_station_id}" for
each leg)`, using real station ids, not `si`. Example: `pl812@269>355~cICE@355>501`. A route key
is valid when it matches `^(pl\d+|sl\d+|c[A-Za-z0-9]+)@\d+>\d+(~(pl\d+|sl\d+|c[A-Za-z0-9]+)@\d+>\d+){0,4}$`,
each leg's alighting station equals the next leg's boarding station, and the first boarding and
last alighting stations equal `from` and `to`.

**K distinct routes** (`routes.plan_routes`), in the manner of Yen's algorithm at the leg level:

```text
plan_routes(tt, fc, source, target, depart_epoch, budget, k=3):
    accepted = []                       # list of (journey_legs, route_key)
    candidates = {}                     # route_key -> [legs, list_of_ban_sets, sort_key]
    queue = [frozenset()]               # ban sets still to search
    searched = set(); n_searches = 0; hit_limit = False
    while len(accepted) < k:
        while queue and n_searches < MAX_SEARCHES and not budget.request_exhausted():
            bans = queue.pop(0)
            if bans in searched: continue
            searched.add(bans); n_searches += 1
            res = earliest_arrival(tt, fc, source, target, depart_epoch, bans, budget)
            hit_limit |= res.hit_limit
            if res.legs is None: continue
            key = route_key(res.legs)
            if key in {rk for _, rk in accepted}: continue
            sk = sort_key(res.legs)     # (forecast arrival, number of legs, -departure epoch)
            if key not in candidates:
                candidates[key] = [res.legs, [bans], sk]
            else:
                candidates[key][1].append(bans)             # remember EVERY ban set that found this route
                if sk < candidates[key][2]: candidates[key][0], candidates[key][2] = res.legs, sk
        if not candidates: break
        key = min(candidates, key=lambda c: candidates[c][2])
        legs, ban_sets, _ = candidates.pop(key)
        accepted.append((legs, key))
        for bans in ban_sets:
            for leg in legs:
                queue.append(bans | {Ban(tt.var_line_key[leg.variant], board_si_of(leg))})
    return accepted, hit_limit, n_searches
```

Every ban set that found a route must be kept, not only the first. In the D15 fixture, the ICE
route is found both with `{pl100@A}` and with `{pl200@B}` banned. Only the child
`{pl200@B, cICE@A}` leads to the third route (RE 1 → RB 4 via C). With 3 routes of about 2 legs
each, this costs 5–8 searches. `MAX_SEARCHES = 10` caps it.

### D10. Latest departure and one route at other times

**Instances of a route** (`routes.route_instances(tt, fc, legs_spec, lo_epoch, hi_epoch)`).
`legs_spec` is a list of `(line_key, board_si, alight_si)`.

1. **First legs.** Take `tt.departures(board_si_0, lo_epoch, hi_epoch)` and keep the runs with
   `var_line_key == line_key_0` whose later usable rows (positions in
   `(position, var_cut)`) include `alight_si_0` with an arrival. Use the **first** such row
   after the boarding position. Forecast them all in one `fc.forecast` call.
2. **Later legs.** For each first leg, in departure order: `cur = forecast arrival at alight_si_0`.
   For each later leg `i`, take the departures at `board_si_i` in
   `[cur + MIN_TRANSFER_S - MAX_DEP_DELAY_S, cur + MAX_WAIT_TRANSFER_S]` with
   `var_line_key == line_key_i` that reach `alight_si_i`. Forecast them as one batch, drop those
   whose `expected_departure` (D8) is before `cur + MIN_TRANSFER_S`, then choose the one with the
   earliest forecast arrival at `alight_si_i`; ties go to the earliest departure. If none exists, this
   first leg gives no instance. Otherwise set `cur` to that arrival.
3. **Deduplicate.** Instances whose legs 2…n use the same runs (the same list of
   `(variant, service_date, board_pos, alight_pos)`) are duplicates. Keep the one whose first leg
   leaves latest. For a 1-leg route, nothing is deduplicated.
4. Sort by first departure and keep at most `TIMINGS_MAX` (24).

**Tightening a found journey** (`routes.tighten`). After `plan_routes`, for each accepted
journey call `route_instances` with `lo = its departure`, `hi = its departure + 2 h`. Among the
instances whose forecast arrival is ≤ the journey's, take the one with the **latest** first
departure. If there is none, which can happen when a later leg's best train differs, keep the
journey as found. Sort the 3 final journeys by `sort_key` again.

**Timings endpoint.** Parse the route key into `legs_spec` (D9 validation), then
`route_instances(…, moment - TIMINGS_HALF_WINDOW_S, moment + TIMINGS_HALF_WINDOW_S)`.

### D11. Constants and limits (`planner/constants.py`)

| Name | Value | Meaning |
|---|---|---|
| `NO_TIME` | -2147483648 | NULL time in int32 arrays |
| `MIN_TRANSFER_S` | 300 | change time |
| `MAX_WAIT_START_S` | 21600 | 6 h at the start |
| `MAX_WAIT_TRANSFER_S` | 10800 | 3 h at a change |
| `MAX_JOURNEY_S` | 57600 | 16 h horizon |
| `MAX_DEP_DELAY_S` | 1800 | maximum carried arrival delay for a connecting departure; also the departure-window lookback |
| `MAX_LEGS` | 5 | trains per journey |
| `MAX_STEP_S` | 21600 | faulty-step threshold (D5) |
| `V_MAX_M_S` | 83.333 | 300 km/h |
| `PROFILE_DAYS` | 28 | profile window |
| `PROFILE_MIN_RUNS` | 2 | runs on a weekday to count as running |
| `ROUTES_K` | 3 | routes returned |
| `MAX_SEARCHES` | 10 | A\* runs per plan |
| `MAX_RUNS_PER_SEARCH` | 25000 | forecast runs per A\* run |
| `SEARCH_SECONDS` | 8.0 | per A\* run |
| `REQUEST_SECONDS` | 20.0 | per plan or timings request |
| `FORECAST_BATCH_WINDOW_S` | 900 | lookahead for batching (D7) |
| `FORECAST_BATCH_MAX_RUNS` | 256 | runs per forecast batch |
| `FORECAST_CACHE_RUNS` | 50000 | module LRU size |
| `TIMINGS_HALF_WINDOW_S` | 7200 | ±2 h |
| `TIMINGS_MAX` | 24 | instances per timings response |
| `PLAN_CONCURRENCY` | 2 | simultaneous plan or timings requests |
| `PLAN_QUEUE_SECONDS` | 30 | wait for a slot before 503 |

`Budget` and the search functions take these as defaults of keyword arguments, so tests can
pass small limits.

### D12. HTTP API

Both views live in `src/dbrel/web/views.py` and follow the existing style:
`JsonResponse({"error": ...}, status=...)` for errors, `read_scope()` for the session, and
`_chosen_moment(request)` to parse `date` and `time`. Both are required here; if
`_chosen_moment` returns `(None, None)`, answer 400 `"date and time are required"`. Routes go in
`urls.py` **before** the `re_path` catch-all:

```python
path("api/plan", views.plan, name="plan"),
path("api/plan/timings", views.plan_timings, name="plan-timings"),
```

**Validation order** (first failure wins):

1. `from` and `to` must be positive integers, else 400 `"from and to must be station ids"`.
2. `from == to` → 400 `"from and to must be different stations"`.
3. `date`/`time` → 400 with `_chosen_moment`'s message. A supplied date with missing
   time reports `"time must be HH:MM"`, as required by the public missing-time scenario.
4. For timings, `route` must be a valid route key for these stations (D9), else 400
   `"route is not a valid route key"`.
5. An unknown station (an id not in the timetable's `si_of_id`; every station row is loaded) → 404 `"no station <id>"`.
6. No current model → 503 `"no delay model is available"`.
7. No free slot within `PLAN_QUEUE_SECONDS` → 503 `"the planner is busy, try again"`.

Use a module-level `threading.BoundedSemaphore(PLAN_CONCURRENCY)`.

**Moment** (used everywhere): `{"epoch": int, "date": "YYYY-MM-DD", "time": "HH:MM"}`, both in
Europe/Berlin (`datetime.fromtimestamp(epoch, TZ)`).

**Forecast**: `{"p25_min": float, "p50_min": float, "p75_min": float}` or `null`.

**Leg**:

```json
{"train_id": 2259, "name": "RE1", "number_name": "RE 10123", "category": "RE",
 "line_key": "pl812",
 "from": {"id": 269, "name": "Köln Hbf"}, "to": {"id": 355, "name": "Hamm (Westf) Hbf"},
 "departure": Moment, "arrival": Moment, "expected_arrival": Moment,
 "forecast": Forecast, "n_stops": 6}
```

- `name` is `queries.passenger_name(session, train)`.
- `number_name` is `train.display_name or f"#{train.id}"`.
- `arrival` is scheduled; `expected_arrival` applies D6.
- `n_stops = alight_pos - board_pos`.

**Change**: `{"station": {"id", "name"}, "planned_min": int, "left_min": int, "at_risk": bool,
"miss_chance": "low" | "medium" | "high" | "very_high" | null}`.

**Journey**:

```json
{"route_key": "pl812@269>355~cICE@355>501",
 "departure": Moment, "arrival": Moment, "expected_arrival": Moment,
 "delay_min": 6, "forecast": Forecast, "duration_min": 274,
 "n_changes": 1, "at_risk": false, "miss_chance": "low", "legs": [Leg], "changes": [Change]}
```

- `departure` is the first leg's departure.
- `arrival`, `expected_arrival` and `forecast` are the last leg's.
- `delay_min = round((expected_arrival.epoch - arrival.epoch) / 60)`.
- `duration_min = round((expected_arrival.epoch - departure.epoch) / 60)`.

**`GET /api/plan`** → 200:

```json
{"from": {"id", "name"}, "to": {"id", "name"}, "date": "2026-10-20", "time": "08:00",
 "model_stamp": "20261014T114610Z", "data_until": "2026-09-28",
 "journeys": [Journey], "reason": null,
 "stats": {"searches": 5, "runs_forecast": 1834, "ms": 2310}}
```

`reason` is null when at least one journey is listed. Otherwise it is the first that applies
of: `from_unserved` (the start station has no rows in `dep_rows`), `to_unserved` (no row of any
variant arrives at the destination; precompute a boolean array `si_has_arrival`),
`search_limit` (any search hit a limit), `no_connection`.

**`GET /api/plan/timings?from&to&date&time&route`** → 200:

```json
{"from": {...}, "to": {...}, "route_key": "...", "date": "...", "time": "...",
 "model_stamp": "...", "data_until": "...",
 "window": {"from": Moment, "to": Moment},
 "instances": [Journey], "stats": {...}}
```

`stats` is excluded from deployed-vs-collection parity comparisons.

### D13. Warm-up and memory

`wsgi._warm` gains a second step after `warm_model`: open a `read_scope()` and call
`planner.timetable.get_timetable(session)`, so the first plan does not pay for the index build.
Log failures and do not raise, as now. The first plan still pays for lines' directions; D6's
shared direction cache makes the second plan cheaper. Task 9 measures both, and memory, on
`data/deploy/slim.sqlite`. The results go to the user before any deploy.

### D14. Frontend

**Files.**

- `src/routes/plan.tsx`: `createFileRoute("/plan")` with `validateSearch` for `from`, `to`
  (positive ints), `date` (`/^\d{4}-\d{2}-\d{2}$/`), `time` (`/^\d{2}:\d{2}$/`) and `option`
  (0, 1 or 2). Types are `?: T | undefined` (tsconfig has `exactOptionalPropertyTypes`). It
  renders `ShellV2` + `PlannerV2`, copying `routes/train/$trainId.tsx`. `head`: title
  "Reiseplaner – BahnPredict".
- `src/components/v2/StationPicker.tsx`: one station field with a suggestion list, modelled on
  the inline dropdown in `SearchV2.tsx` (lines ~446–511 and the outside-click effect at
  ~358–367). Props:
  `{ inputId: string; label: string; icon: IconKind; value: {id: number; name: string} | null; onChange: (v: {id: number; name: string} | null) => void; t: Copy }`.
  Typing clears `value` (calls `onChange(null)`) and shows suggestions from
  `useSuggest(query, undefined, "station")`. Choosing one calls `onChange({id, name})` and puts
  the name in the input. Use a unique listbox id per instance
  (`${inputId}-suggestions`). Do **not** refactor `SearchV2.tsx`.
- `src/components/v2/PlannerV2.tsx`: the form, the results, and the composition of the next two.
- `src/components/v2/JourneyCard.tsx`: one connection entry (button).
- `src/components/v2/JourneyTimings.tsx`: the other-times list with Earlier/Later.
- `src/components/v2/JourneyLegs.tsx`: the legs and changes of one instance.

**State model in `PlannerV2`.**

- `form = {from, to, date, time}` lives in `useState`, initialised from the URL search params.
  Station names are unknown at first; fill each name from the plan response's `from.name` /
  `to.name` when the form's station has that id and an empty name.
- `submitted = URL has from, to, date, time`. The plan query (`usePlan`) runs only when
  `submitted`, with the URL values.
- Results are shown only while the form equals the URL values (`from.id`, `to.id`, `date`,
  `time`). Editing the form therefore hides them, which is the spec's "withdraw", and needs no
  extra state.
- Pressing the button: `navigate({ to: "/plan", search: { from, to, date, time } })`. This
  drops `option`.
- Clicking connection `i`: `navigate({ to: "/plan", search: { ...current, option: i }, replace: true })`.
- `JourneyTimings` gets `key={journey.route_key}`, so its local `shift` state resets when the
  chosen route changes.

**Hooks and API** (`lib/api.ts`, `hooks/use-dbrel.ts`).

- Add TypeScript types mirroring D12 exactly: `Moment`, `PlanForecast`, `PlanLeg`, `PlanChange`,
  `Journey`, `PlanResponse` and `TimingsResponse`. Do not reuse the name `RouteRef`.
- Add `plan(from, to, date, time)` → `GET /api/plan` and
  `planTimings(from, to, date, time, route)` → `GET /api/plan/timings`.
- Add `usePlan(params | undefined)` and `usePlanTimings(params | undefined)` with `useQuery`.
  Key them on every param, with `staleTime: 5 * 60_000` and `retry: false`; a 503 must not be
  retried 3 times.
- Give `useSuggest` an optional third parameter `only?: "station" | "line" | "train"`, passed to
  `api.suggest` and added to the query key. Existing callers are unchanged.

**Timings window.** `moment = journey.departure.epoch + shift * 4 * 3600`. Turn it into a Berlin
`date` and `time` with a new helper in `run-format.ts`:
`berlinMoment(epoch: number): {date: string; time: string}`, using
`Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Berlin", year: "numeric", month: "2-digit",
day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })` and `formatToParts`.
Earlier sets `shift - 1` and Later sets `shift + 1`. The highlighted instance is the one whose
legs' `train_id`s and `departure.epoch` equal the clicked journey's. The chosen instance for
`JourneyLegs` is local state, defaulting to the highlighted one, else index 0.

**Layout** (reuse the classes in `SearchV2.tsx` and `RunEntry.tsx`). The connection entries,
the timings window and list, and `JourneyLegs` below are superseded by D16; the rest still holds.

- **Form card**, like the search card (`rise rounded-lg border border-line bg-panel p-4 sm:p-5`):
  - phone: one column — From, a swap button (`icons.swap`, `aria-label` = t.swapStations), To,
    then a row with date and time (each with Today/Now quick buttons), then the full-width
    submit button;
  - from `sm`: From and To side by side with the swap button between them.
  - Fields use `FIELD_CLASS`. Copy the constant into the new file; do not export it from
    `SearchV2`.
- **Results card**: a header with the from → to names, the date, and a small line
  `t.dataUntil` + `t.forecastNote`. Under it, up to 3 `JourneyCard`s separated by
  `divide-y divide-line`.
- **`JourneyCard`**: `<button>` with fixed height `h-[5.75rem]` (`RUN_ROW_HEIGHT_TITLED`),
  `aria-pressed={chosen}`, and a teal left border + `bg-ink` when chosen. Three lines, each one
  line with `truncate`:
  1. `departure.time → expected_arrival.time` in `font-display font-semibold`, then
     `<DelayText>` for the destination delay. Pass `forecast` and the scheduled `arrival.time`.
     If `forecast` is null, show `t.noPrediction`.
  2. `t.plannedArr` with `arrival.time` · duration as "4 h 34 min" (helper
     `formatDuration(min, locale)` in `run-format.ts`) · `t.direct` / `t.oneChange` /
     `t.nChanges`.
  3. The legs' `name`s joined by " › ", and at the right, if `at_risk`, `icons.risk` in
     `text-amber` + `t.tightChange`.
- **`JourneyTimings`** card:
  - Title `t.otherTimes`, and `RunColumnHeader`-like labels (departure, expected arrival,
    delay).
  - Rows are `<button>`s with fixed height `RUN_ROW_HEIGHT` (`h-[4.25rem]`) in a two-line grid:
    1. dep time · expected arrival · `DelayText`;
    2. `formatRunDay(departure.date)` · duration · changes · risk tag.
  - The highlighted row gets a teal left border.
  - Earlier/Later buttons sit beside the list from `sm` and under it on a phone, as in the
    Prognose card. Reuse the existing Earlier/Later strings; find them with
    `grep -n "Früher\|Später" src/lib/i18n.ts`.
  - Loading shows 4 skeleton rows of the same height.
  - Empty shows `t.noTimings`.
- **`JourneyLegs`**: for each leg, a block with:
  - the train `name` as a `<Link {...v2Paths.train(leg.train_id, leg.from.id)}>` plus
    `<TrainNumber name number_name>`;
  - "from.name · departure.time";
  - "to.name · arrival.time → expected_arrival.time" with `DelayText`.

  Between legs, a change row: `icons.change`, `t.changeAt` with the station, `t.minutesLeft`,
  and the risk tag. This panel may be any height.
- **States**: loading → 3 `Skeleton` blocks of `h-[5.75rem]` + `t.searchingSlow`. The 503
  "no delay model" error → `t.noModel`. Other errors → `t.error`. An empty list → the text for
  `reason`.

**Header link** (`ShellV2.tsx`): a `<Link to="/plan">` between the logo and the toggles.

- It shows `icons.planner` and `t.planner` (hide the text below `sm` with
  `hidden sm:inline`) and has `aria-label={t.planner}`.
- It gets `aria-current="page"` and teal text when the current path is `/plan`
  (`useRouterState({ select: s => s.location.pathname })`).
- `ShellV2` gets `t` from `copy[locale]`, as other components do.

**Icons** (`icons.ts`): add `from: CircleDot`, `to: Goal`, `swap: ArrowUpDown`,
`planner: Waypoints`, `duration: Timer`, `change: Repeat`, `risk: TriangleAlert`. Before using
one, check that it exists:

```
grep -c "declare const Goal\b" node_modules/lucide-react/dist/lucide-react.d.ts
```

If it does not, use `Flag` for `to`, `Route` for `planner`, `Clock` for `duration` and
`ArrowLeftRight` for `change`.

**Paths** (`paths.ts`): add
`plan: (s?: {from?: number; to?: number; date?: string; time?: string; option?: number}) => ({ to: "/plan", search: s ?? {} }) as const`.
Run `npm run build` (or `vite dev`) once after adding `routes/plan.tsx`, so that
`routeTree.gen.ts` is regenerated before `"/plan"` type-checks. Commit the regenerated file with
the change.

**Strings** (`lib/i18n.ts`, both locales, same keys):

| key | de | en |
|---|---|---|
| `planner` | Reiseplaner | Journey planner |
| `fromStation` | Von | From |
| `toStation` | Nach | To |
| `stationPlaceholder` | Bahnhof suchen | Search station |
| `swapStations` | Start und Ziel tauschen | Swap start and destination |
| `findConnections` | Verbindungen suchen | Find connections |
| `searchingSlow` | Die Suche kann bis zu 20 Sekunden dauern. | The search can take up to 20 seconds. |
| `noConnection` | Keine Verbindung innerhalb der Suchgrenzen gefunden. | No connection found within the search limits. |
| `fromUnserved` | An diesem Startbahnhof halten keine Züge. | No trains stop at this start station. |
| `toUnserved` | An diesem Zielbahnhof halten keine Züge. | No trains stop at this destination. |
| `searchLimit` | Die Suche hat ihr Zeitlimit erreicht. Versuche eine spätere Uhrzeit oder nähere Bahnhöfe. | The search hit its time limit. Try a later time or closer stations. |
| `noModel` | Kein Prognosemodell verfügbar. | No forecast model is available. |
| `direct` | Direkt | Direct |
| `oneChange` | 1 Umstieg | 1 change |
| `nChanges` | {n} Umstiege | {n} changes |
| `plannedArr` | planm. {time} | sched. {time} |
| `tightChange` | Umstieg gefährdet | Tight change |
| `otherTimes` | Diese Route zu anderen Zeiten | This route at other times |
| `noTimings` | Keine weiteren Fahrten dieser Route in diesem Zeitraum. | No other runs of this route in this window. |
| `changeAt` | Umstieg in {station} | Change at {station} |
| `minutesLeft` | {n} Min. zum Umsteigen | {n} min to change |
| `dataUntil` | Fahrpläne gesehen bis {date} | Timetables seen up to {date} |
| `forecastNote` | Ankunftszeiten sind Prognosen des Modells. | Arrival times are model forecasts. |
| `legsTitle` | Fahrtverlauf | Journey details |

### D16. Readability revision (2026-10-16)

User feedback on the first build: the Scheduled / Delay / Expected columns did not say what the
other-times list measured, the journey details were hard to read, and choosing a connection made
the other-times card vanish and reappear and pulled the page up. This section replaces the
corresponding parts of D14.

**One entry for both lists** (`JourneyRow.tsx`, `JourneyColumnHeader`). Fixed height
`JOURNEY_ROW_HEIGHT` (`h-[4.5rem]`), two lines, grid
`[1.2fr 0.8fr 1fr 0.75rem]` on a phone and `[1.3fr 1fr 1fr 1rem]` from `sm`:

| column | header (long / short) | line 1 | line 2 |
|---|---|---|---|
| 1 | `colTrip` / `colTripShort` | `departure.time → arrival.time` (scheduled) | connections: train links › … · duration; other times: run day · duration |
| 2 | `colForecastArrival` / `colForecastShort` | `expected_arrival.time` in its delay colour | `delayLabel`: "+22 Min." or `arrivesOnTime` |
| 3 | `colChangeRisk` / `colChangeRiskShort` | `MissChanceTag` (band in teal / amber / red), or `noChangeNeeded` | change stations as board links |

The long headers show from `sm`, the short ones below it. `MissChanceTag` hides its icon below
`sm` so that "unter 25 %" fits. `RiskTag` and `tightChange` are no longer used by the planner.

**Other times** (`JourneyTimings.tsx`). No `key` on the component; it holds
`{route, origin, centres, first, picked}` and resets that state during render when the chosen
journey's `route_key` or departure changes, so the card stays mounted. `usePlanTimingWindows`
(replaces `usePlanTimings`) runs one `useQueries` entry per centre epoch and merges the
instances, deduplicated by first departure + train ids and sorted by departure.

- `ROWS = 5` entries. With `first === null` the view starts 2 entries before the clicked journey
  (clamped to the loaded list); otherwise at the first instance departing at or after `first`.
- Prefetch: when no window is loading and fewer than `ROWS` instances lie after the view, add a
  centre at `hi + 2 h` (until `hi` reaches origin + 24 h); else when fewer than `ROWS` lie before
  it, a centre at `lo − 2 h` (until origin − 24 h). One window at a time.
- Later sets `first` to the instance after the last shown; Earlier to the instance `ROWS` before
  the first shown. Each is disabled while no such instance is loaded.
- The list renders exactly `ROWS` slots: an entry, a skeleton while loading, or empty space.
  Error and empty texts are laid over the slots. The slide animation of the first build is gone.
- The header shows `otherTimesHint` in place of the window's time span.

**Prefetch of every connection.** `usePrefetchPlanTimings` in `PlannerV2` calls
`queryClient.prefetchQuery` with the same query definition as `usePlanTimingWindows`
(`planTimingsQuery`), for the first window of each listed connection, sequentially, chosen one
first. A window already loading or fresh is not requested twice.

**No scroll on choosing a connection.** The `navigate` that sets `option` passes
`resetScroll: false`.

**Journey details** (`JourneyLegs.tsx`). Header: from → to names, then day, `scheduledShort`
dep → arr, `forecastShort` expected arrival, duration, changes, and `legsHint`. Each leg is a
bordered block: a link row (train icon, name, `TrainNumber`, `toStationShort`, chevron) to the
train page; then two stop lines `time · icon · station link`, the arrival line adding
"`forecastShort` <expected> (<delayLabel>)" (third column from `sm`, own line below). Between
legs, a dashed change block with `changeAt`, `changeTime` (planned and expected minutes) and,
under `missChanceLabel`, the `MissChanceTag`.

**Form.** Date and time each sit in a column with its quick button directly below
(`grid-cols-2`, `space-y-2`).

**Frequent stations** (`lib/frequent-stations.ts`). localStorage key `dbrel.planner.stations`,
an array of `{id, name, count}`, the 30 most used kept. `recordStations` runs on submit for both
stations, skipping one whose name is not yet known (a form restored from the URL before the plan
answered). `StationPicker` reads the list when it opens: with an empty query it shows the top 6
under `frequentStations`; with a query, `frequentFirst` puts up to 3 entries whose name contains
it (`toLocaleLowerCase("de")`) before the server's suggestions, without repeats. Every storage
access is in `try/catch`.

**New strings** (both locales): `frequentStations`, `colTrip`, `colForecastArrival`,
`colChangeRisk`, `colTripShort`, `colForecastShort`, `colChangeRiskShort`, `missLow`,
`missMedium`, `missHigh`, `missVeryHigh`, `missUnknown`, `noChangeNeeded`, `arrivesOnTime`,
`minutesLate`, `otherTimesHint`, `legsHint`, `scheduledShort`, `forecastShort`, `changeTime`,
`missChanceLabel`. (`onTime` was taken by the on-time-probability label, hence
`arrivesOnTime`.)

### D15. Test fixture for the search

`tests/planner_fixture.py` builds a `PlannerTimetable` with `from_columns`, without a database.
Its stations are spaced about 34 km apart along latitude 51.0:

| id | name | lon |
|---|---|---|
| 1 | A | 7.0 |
| 2 | B | 7.5 |
| 3 | C | 8.0 |
| 4 | D | 8.5 |
| 9 | Z | 9.0 (no stops) |

Trains (service-day seconds written as clock times; "daily" means a profile with 4 runs on
every weekday):

| train | line key | days | stops (arr/dep) |
|---|---|---|---|
| 100 RE 1 | pl100 | daily | A –/08:00, B 08:30/08:31, C 09:00/– |
| 102 RE 1 | pl100 | daily | A –/07:30, B 08:00/08:01, C 08:30/– |
| 200 RE 2 | pl200 | daily | B –/08:36, D 09:20/– |
| 201 RE 2 | pl200 | daily | B –/08:50, D 09:34/– |
| 300 ICE 300 | cICE | Mon–Fri | A –/08:10, D 09:30/– |
| 400 RB 4 | pl400 | daily | C –/09:10, D 09:40/– |
| 500 RE 5 | pl500 | daily | A –/10:00, B 10:30/10:31, C 34:00/– (faulty) |
| 600 RE 6 | pl600 | daily | A –/23:50, B 24:20/24:21, D 25:00/– |
| 700 RE 7 | pl700 | Saturdays only | A –/08:05, D 09:00/– |

Dates: 2026-10-19 is a Monday, 2026-10-18 a Sunday, and 2026-10-03 a Saturday that is a
holiday in every state. The fixture's `holiday_set` contains `("2026-10-03", state)` for all
16 states.

A second fixture, `make_late_connection_timetable()`, has only these two trains (same stations,
daily profiles). It tests the carried-over delay of D8:

| train | line key | stops (arr/dep) |
|---|---|---|
| 100 RE 1 | pl100 | A –/08:00, B 08:30/08:31, C 09:00/– |
| 250 RE 25 | pl250 | C –/07:50, B 08:35/08:36, D 09:20/– |

The expected results for these fixtures are listed in task groups 5 and 6.

## Risks / Trade-offs

- **[Cold first plan is slow]** Directions per line and the notice rates are computed on first
  use. → The warm-up thread builds the index. The direction cache is shared across requests.
  Task 9 measures the cold and warm times, and the user decides whether to precompute
  directions.
- **[Memory on the 1 GB Fly machine]** The index is about 60–100 MB, plus onnxruntime and the
  model, about 100 MB. → Measure peak and steady RSS (including swap) in task 9. Report before
  deploying. `PLAN_CONCURRENCY = 2` bounds the per-request memory.
- **[Running profile mistakes]** Seasonal or one-off trains, and timetable changes after
  `data_until`. → `data_until` is shown to the user. `PROFILE_MIN_RUNS = 2` drops one-off runs.
- **[Independent per-stop forecasts]** A change's risk is a simple p75 test, not a probability.
  → Labelled "Tight change", not a percentage.
- **[Departures assumed on time]** A delayed connecting train would in reality make a change
  easier. → This is the conservative choice, documented in the spec.
- **[Heuristic inconsistency on bad data]** → At worst slightly suboptimal results. Faulty steps
  are cut (D5).
- **[Same station, different ids]** Without walking links, Hbf and "Hbf (tief)" are not
  connected. → Listed as a non-goal; a follow-up change can add walking links.
- **[Two code paths for the profile]** → One shared SQL string, and the build compares the
  stored and computed profiles row by row.

## Migration Plan

1. Archive `improve-v2-search-and-prognose` (task 0.1).
2. Implement and test locally against the collection database **and** a rebuilt
   `data/deploy/slim.sqlite`.
3. Before rebuilding, copy the current slim file to
   `data/deploy/slim-backup-184MiB-before-running-profile.sqlite`. The user keeps backups.
4. Report measurements (task 9) and wait for the user. **Do not commit or push.** A push to
   main deploys to Fly. The deployed DB on the Fly volume must be replaced with the rebuilt
   file in the same release, because the new server code expects the profile table. Without it,
   the planner returns empty results with `from_unserved`; other pages keep working.
5. Rollback: redeploy the previous commit. The extra table in the new DB file is ignored by old
   code.

## Open Questions

- Should directions be precomputed at deployed-build time if the cold first plan is too slow?
  This is decided after task 9's numbers and does not change the specs.
