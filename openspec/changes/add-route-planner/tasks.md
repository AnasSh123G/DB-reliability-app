# Tasks

**Read before starting.** This change is written for step-by-step execution. `design.md`
section numbers (D1–D15) are referenced throughout. Read the whole design once before task 1.1.

**Standing rules for every group:**

- **Never commit or push.** A push to `main` deploys to Fly. Leave every change uncommitted and
  report at the end.
- **Run the backend tests with `uv run pytest -q <files>`** from the repo root. Run the frontend
  checks from `bahn-buddy-predict/`.
- **Anything that loads the 51 GB collection database or the whole slim database** (a deployed
  build, the parity comparison, the benchmark) goes through the capped launcher:
  `uv run dbrel ml launch <name> --memory <cap> -- <command>`. Never run it in the foreground
  of the editor's terminal. The local web server (`dbrel serve`) is the one exception. Report memory as RSS **plus swap** (`VmRSS` + `VmSwap` in
  `/proc/<pid>/status`).
- **Fixed height.** After each UI task, measure the heights of the planner's timings entries
  (`[data-timing]`) and connection entries (`[data-journey]`) in headless Chromium at 1280 px and
  390 px. Each kind must have one height in every case.
- **If a test or check fails and the cause is not obvious within two attempts, stop.** Report
  the failing output to the user. Do not weaken a test to make it pass.
- **Do not change** `SearchV2.tsx`, `BoardV2.tsx`, `TrainV2.tsx`, `LineV2.tsx`, anything under
  `routes/v1/`, or the model artifacts.

## 0. Prerequisites

- [x] 0.1 Ask the user to confirm archiving `improve-v2-search-and-prognose` (all its 42 tasks are done). Once they confirm, run `openspec archive improve-v2-search-and-prognose`. Verify that `openspec list --json` no longer lists it and that `grep -c "The Prognose card steps through runs" openspec/specs/web-frontend-v2/spec.md` prints 1. Then run `openspec validate add-route-planner --strict` and verify it prints "valid". If it reports that a MODIFIED requirement header is not found, stop and report.
- [x] 0.2 Copy `data/deploy/slim.sqlite` to `data/deploy/slim-backup-184MiB-before-running-profile.sqlite` (`cp --reflink=auto`). Verify that both files have the same size with `ls -l`. Never delete any `slim-backup-*` file.

## 1. Running profile (`src/dbrel/planner/`)

- [x] 1.1 Create `src/dbrel/planner/__init__.py` (a one-line docstring: "Journey planner: A* over forecast arrival times.") and `src/dbrel/planner/constants.py` with every constant of design D11, using exactly those names and values, each with a one-line comment. Verify that `uv run python -c "import dbrel.planner.constants as c; print(c.MIN_TRANSFER_S, c.MAX_LEGS, c.NO_TIME)"` prints `300 5 -2147483648`.
- [x] 1.2 Create `src/dbrel/planner/profile.py` with the following, all as in D3. Verify that `uv run python -c "import dbrel.planner.profile"` succeeds.
  - `PROFILE_SQL` (a format string with `{trip}`, `{holiday}`, `{train}` and the named parameters `:first`, `:last`, `:cutoff`);
  - `profile_params(conn, trip, train) -> dict | None` (computes `last` = `MAX(service_date)` of trip, `first` = last − 27 days, `cutoff` = `MAX(last_seen_date)` of train − `ACTIVE_WINDOW_DAYS`; returns None when trip is empty). Import `ACTIVE_WINDOW_DAYS` from where `queries.py` gets it (`grep -n ACTIVE_WINDOW_DAYS src/dbrel/web/queries.py src/dbrel/config.py`);
  - the `RunProfile` dataclass;
  - `load_run_profile(session)` (the deployed path first, then the collection path, then empty);
  - `NOTHING = object()`, `day_key(service_date, state, holiday_set)` and `chosen_variants(profile)`.
- [x] 1.3 Write `tests/test_planner_profile.py`, using an in-memory SQLite engine with `Base.metadata.create_all`, as `tests/test_views.py` does. Insert `Station`, `Train` and `Trip` rows directly. Verify that `uv run pytest -q tests/test_planner_profile.py` passes. Tests:
  - a train with trips on every Monday–Friday of the 28 days ending 2026-09-27 gets counts `(4,4,4,4,4,0,0)`;
  - a trip 40 days before the newest date is not counted;
  - a date with a `public_holiday` row with partial = 0 (any state) is left out, and one with partial = 1 is not;
  - a train whose `last_seen_date` is older than newest − 14 days has no counts;
  - the weekday number is pinned to real dates (2026-09-07 → 0, 2026-09-13 → 6), as `tests/test_views.py` does;
  - `day_key`: a Monday → 0, a Sunday → 6, 2026-10-03 with state "DE-NW" and that holiday in `holiday_set` → 6, and the same date with state None when only one state has it → 5;
  - `chosen_variants`: a variant with 1 run on Monday is not chosen (`NOTHING`); of two qualifying variants, the one with more runs on that weekday wins; on equal runs, the larger total wins.

## 2. Running profile in the deployed database

- [x] 2.1 In `src/dbrel/deployed.py`, next to `VARIANT_RUN_DATA`, add:
  - the `variant_weekday_data` and `running_profile_window` tables (on `metadata`, as in D3);
  - a `TRAIN_VARIANT_WEEKDAY` Table on `view_metadata`, columns `train_id`, `path_sha1`, `weekday`, `n_runs`, like `TRAIN_VARIANT_RUN`;
  - the `TRAIN_VARIANT_WEEKDAY_VIEW` SQL string.

  First check that `variant_path` has a row with `id = 0` for the NULL hash: `sqlite3 -readonly data/deploy/slim.sqlite "select * from variant_path where id=0"`. If it has none, do what `TRAIN_VARIANT_RUN_VIEW` does for NULL paths, and write down which way it works. Verify that `uv run pytest -q tests/test_deployed_build.py tests/test_deployed_runs.py` still passes.
- [x] 2.2 In `src/dbrel/deployed_build.py`, right after the `variant_run_data` insert (around line 270):
  - compute the parameters with `profile_params` on the source connection;
  - insert into `variant_weekday_data` with `SELECT g.train_id, COALESCE(vp.id, 0), g.weekday, g.n_runs FROM (<PROFILE_SQL formatted with trip='src.trip', holiday='src.public_holiday', train='src.train'>) g LEFT JOIN variant_path vp ON vp.path_sha1 = g.path_sha1`;
  - insert the one `running_profile_window` row and create the view in the same loop as the other views;
  - store `counts["running_profile"]`.

  Verify by building the `tests/ml_fixture.py` database the way `tests/test_deployed_build.py` does, and checking that the new view has rows.
- [x] 2.3 Still in `deployed_build.py`, after the run-summary verification, add a `_verify("running profile", ("train_id", "path_sha1", "weekday", "n_runs"), 3, <source rows from PROFILE_SQL with plain names on the source connection>, <SELECT … FROM train_variant_weekday ORDER BY train_id, path_sha1, weekday>)` call. Copy how `_verify` is used for "run summary" (line ~289); the `key` argument is the number of leading key columns (3 here). Add a log line `running profile: <n> rows, window <first>..<last>`. Verify that the build of the fixture database logs that line.
- [x] 2.4 Extend `tests/test_deployed_build.py` with these tests. Verify that `uv run pytest -q tests/test_deployed_build.py` passes.
  - (a) `load_run_profile` on the built deployed file equals `load_run_profile` on the fixture collection database: same counts, same first and last date;
  - (b) a retired train has no `variant_weekday_data` rows;
  - (c) a profile mismatch fails the build with `DeployedBuildError` and leaves the output path unchanged. To provoke it, monkeypatch the verification's source query, the same way an existing mismatch test does; find one with `grep -n DeployedBuildError tests/`.

## 3. Timetable index (`src/dbrel/planner/timetable.py`)

- [x] 3.1 Implement in `timetable.py`, exactly as in D4 and D5. Verify that `uv run python -c "import dbrel.planner.timetable"` succeeds.
  - the `Departure` dataclass and the `PlannerTimetable` class with the attributes of the D4 table;
  - `PlannerTimetable.from_columns(*, stations, train_id, path_code, path_values, stop_sequence, station_id, arr, dep, chosen, line_key_of_train, holiday_set, first_date, last_date)`, which performs D4 steps 5–9. Here `stations` is a list of `(id, name, lat, lon, state)`, `path_code` an int32 array indexing `path_values`, and `arr`/`dep` int32 arrays with `NO_TIME`;
  - the `var_cut` computation of D5;
  - `departures(si, lo_epoch, hi_epoch)`;
  - `si_has_arrival` (bool array: some usable row with an arrival exists at that station);
  - `nbytes()` (the sum of the array sizes).
- [x] 3.2 Implement `load_timetable(session)` (D4 steps 1–4 and 10, then `from_columns`) and `get_timetable(session)` (a module dict keyed by `str(session.get_bind().url)`, guarded by a `threading.Lock`, built once). Also implement the line keys of D9 (`pl…`/`sl…`/`c…`). Read `passenger_line_member` only if `sqlite_master` lists that table and it has rows. Verify that `uv run python -c "import dbrel.planner.timetable"` succeeds. The real-data check comes in 3.5.
- [x] 3.3 Create `tests/planner_fixture.py` with `make_timetable()`, which returns the D15 timetable through `from_columns`. Give it daily profiles (`chosen` lists all 7 day keys), ICE 300 Monday to Friday only (day keys 0–4), RE 7 Saturdays only (day key 5), and `holiday_set` holding `("2026-10-03", s)` for all 16 German state codes (`DE-BW`, `DE-BY`, `DE-BE`, `DE-BB`, `DE-HB`, `DE-HH`, `DE-HE`, `DE-MV`, `DE-NI`, `DE-NW`, `DE-RP`, `DE-SL`, `DE-SN`, `DE-ST`, `DE-SH`, `DE-TH`). All stations have state `DE-NW`. Also add `make_late_connection_timetable()` (the second table in D15). Write times with a helper `hm("08:30") -> 30600` that accepts hours ≥ 24. Verify that `uv run python -c "import sys; sys.path.insert(0, 'tests'); import planner_fixture as f; print(f.make_timetable().stop_si.size)"` prints 22 (the number of stop rows in the D15 table).
- [x] 3.4 Write `tests/test_planner_timetable.py`. Verify that `uv run pytest -q tests/test_planner_timetable.py` passes. Tests:
  - RE 5's `var_cut` is 2 (stops A and B usable, C not);
  - departures at A on 2026-10-19 from 07:00 to 09:00 are RE 102 07:30, RE 100 08:00 and ICE 300 08:10, in that order;
  - the same window on Sunday 2026-10-18 lacks ICE 300;
  - departures at A on Saturday 2026-10-10 include RE 7 at 08:05, and on 2026-10-03 (the holiday) do not;
  - departures at B on 2026-10-20 from 00:10 to 01:00 contain exactly RE 6, with `service_date` 2026-10-19 and a `dep_epoch` that is 00:21 local on 2026-10-20;
  - `si_has_arrival` is false for Z and for A (nothing arrives at A), and true for D.
- [x] 3.5 Real-data smoke test (no pytest). Launch `uv run dbrel ml launch planner-index --memory 3G -- env DBREL_DB=data/deploy/slim.sqlite PYTHONPATH=src .venv/bin/python -c "<build with get_timetable in a Session over make_engine(DB_PATH), print N, V, S, D, nbytes, seconds and peak RSS from resource.getrusage>"`, then read its journal (`journalctl --user -u dbrel-ml-planner-index`). This must run **after** group 2 has rebuilt the slim file, so do it at task 8.2. Until then, run it against the collection database with `DBREL_DB=data/dbrel.sqlite` and `--memory 6G`. Verify that it finishes. Record the numbers for task 9.2.

## 4. Forecasts (`src/dbrel/planner/forecast.py`)

- [x] 4.1 Implement, exactly as in D6. Verify that `uv run python -c "import dbrel.planner.forecast"` succeeds.
  - the `ArrivalForecaster` Protocol;
  - the `_Row` NamedTuple (`stop_sequence`, `planned_arrival_sec`, `planned_departure_sec`);
  - `delay_s(p50)`;
  - `expected_departure(tt, arr, anchor, base, pos)` (D8: scheduled departure plus the run's own forecast arrival delay at that row capped at `MAX_DEP_DELAY_S`;  0 when the row has no arrival or the forecast is NaN);
  - the module LRU `_FORECAST_CACHE` with its lock and `FORECAST_CACHE_RUNS`;
  - `ModelForecaster`, which imports `_ModelDirectionCache` from `dbrel.web.queries` inside the constructor;
  - `FixedForecaster`, with `calls` and `runs_forecast` counters and a `keys` list of every run it was asked for.
- [x] 4.2 Write `tests/test_planner_forecast.py`. Verify that `uv run pytest -q tests/test_planner_forecast.py` passes. Tests:
  - (a) `FixedForecaster` returns an array of shape `(var_len, 3)` with NaN on rows that have no arrival;
  - (b) `delay_s(-2.0) == 0`, `delay_s(4.0) == 240` and `delay_s(nan) == 0`;
  - (b2) `expected_departure` for RE 25 at B (arrival 08:35, departure 08:36) with p50 = 4 gives 08:40; with p50 = 0 or NaN gives 08:36; for a row with no arrival (RE 200 at B) gives 08:36 whatever the array holds;
  - (b3) `expected_departure` for RE 25 at B with p50 = 30 or 45 minutes gives 09:06 in both cases (the 30-minute cap); a negative median gives 08:36. Arrival delay calculations remain uncapped: `delay_s(45.0) == 2700`;
  - (b4) two distinct timetables with the same model stamp, numeric variant index and service date never share a forecast-cache entry, even when the variants refer to different trains or row layouts; repeated forecasts within one timetable still reuse their entry;
  - (c) the LRU evicts the oldest entry when full (monkeypatch the size to 2);
  - (d) on the `tests/ml_fixture.py` database, `ModelForecaster` builds, for every arrival row of one variant, a `ScheduledCall` equal to `calls_at(session, train, station_id, service_date, variant=path_sha1)` in train id, station id, service date, both planned times, `stop_rank`, `n_stops` and `trip_start_sec`. Monkeypatch `DelayModel` with a fake whose `predict_calls` records its calls and returns `Prediction(call, arr=(1.0, 2.0, 3.0), dep=None)`, so that no real model is needed.

## 5. A\* search (`src/dbrel/planner/search.py`)

- [x] 5.1 Implement `Ban`, `LegRef`, `Label`, `RunScan`, `SearchResult`, `Budget`, `haversine_m`, `heuristic(tt, target_si) -> np.ndarray`, `lower_bound(...)`, `reconstruct(...)` and `earliest_arrival(tt, fc, source_si, target_si, depart_epoch, bans, budget, cache)`, exactly as in D7 and D8. `cache` is the per-request dict of forecasts. Limits come from `Budget`, whose constructor defaults to the D11 constants. Verify that `uv run python -c "import dbrel.planner.search"` succeeds.
- [x] 5.2 Write `tests/test_planner_search.py` against `make_timetable()` and `FixedForecaster`. "08:00 on date X" means `int(datetime(…, tzinfo=TZ).timestamp())`. Verify that `uv run pytest -q tests/test_planner_search.py` passes. Tests:
  - (a) Monday 2026-10-19 08:00, A→D, no delays: legs RE 100 A→B, then RE 200 B→D; the label arrival at D is 09:20;
  - (b) the same, with RE 100 forecast `(4, 4, 4)` at B: the result is ICE 300 A→D, arriving 09:30;
  - (c) Sunday 2026-10-18 08:00, A→D: no leg uses train 300;
  - (d) Saturday 2026-10-10 08:00, A→D: RE 7 direct, arriving 09:00. On 2026-10-03 08:00 RE 7 is not used and the arrival is 09:20;
  - (e) Monday 10:00, A→C: not found (`legs is None`, `hit_limit` false), because RE 5 never reaches C;
  - (f) Monday 10:00, A→B: RE 5 arriving 10:30;
  - (g) Tuesday 2026-10-20 00:10, B→D: RE 6 boarding at 00:21, arriving 01:00 local on 2026-10-20;
  - (h) a forecast of `(-2, -2, -2)` on RE 200 at D still arrives at 09:20, and a NaN forecast also arrives at 09:20;
  - (i) `Budget(max_runs_per_search=1)` gives `hit_limit` true and no legs;
  - (j) banning `Ban("pl100", si_of(A))` gives ICE 300;
  - (k) the requested time 08:00 boards RE 100 leaving at exactly 08:00;
  - (l) on `make_late_connection_timetable()`, Monday 08:00 A→D, RE 100 `(4, 4, 4)` and RE 25 `(4, 4, 4)` at B: found, RE 100 A→B then RE 25 B→D (expected departure 08:40 ≥ 08:34 + 5 min), arriving 09:20 at D;
  - (m) the same with RE 25 `(0, 0, 0)` at B: not found (`legs is None`, `hit_limit` false), because RE 25 leaves at 08:36 and 08:34 + 5 min is 08:39;
  - (n) the same with RE 25 `(1, 1, 1)` at B (leaves 08:37, 08:39 > 08:37): not found.

Additional D7 regressions:
- A later arrival at a station can catch a departure beyond an earlier arrival's three-hour
  wait window; retain that valid route.
- A later arrival with fewer used trains preserves enough legs to reach the destination
  within `MAX_LEGS`.
- Equal forecast destination arrivals and leg counts prefer the later first departure, even
  when the routes board a shared run at different stops.
- Returning to the source station is a change and does not reset the first-departure rules.

## 6. Distinct routes, tightening, other times (`src/dbrel/planner/routes.py`)

- [x] 6.1 Implement the following, as in D9 and D10. Verify that `uv run python -c "import dbrel.planner.routes"` succeeds.
  - `route_key(tt, legs) -> str`;
  - `parse_route_key(tt, key, from_id, to_id) -> list[tuple[str, int, int]] | None`, which returns `(line_key, board_si, alight_si)` per leg or None if invalid, using the D9 regex and connectivity rules;
  - `sort_key(tt, cache, legs)`;
  - `plan_routes(...)`, keeping **every** ban set per candidate, as the D9 note says;
  - `route_instances(...)`;
  - `tighten(...)`.
- [x] 6.2 Write `tests/test_planner_routes.py` against the fixture. Verify that `uv run pytest -q tests/test_planner_routes.py` passes. Tests:
  - (a) Monday 08:00, A→D, no delays: exactly 3 routes in this order:
    - `pl100@1>2~pl200@2>4`, departing 08:00, arriving 09:20;
    - `cICE@1>4`, 08:10–09:30;
    - `pl100@1>3~pl400@3>4`, 08:00–09:40;
  - (b) with RE 100 forecast `(4, 4, 4)` at B, the order is:
    - `cICE@1>4`, arriving 09:30;
    - `pl100@1>2~pl200@2>4` via RE 201, arriving 09:34;
    - `pl100@1>3~pl400@3>4`, arriving 09:40;
  - (c) Sunday 2026-10-18 08:00: exactly 2 routes (`pl100@1>2~pl200@2>4` and `pl100@1>3~pl400@3>4`);
  - (d) Monday 07:00: the first route departs at 08:00 (RE 100), not 07:30 (tightening);
  - (e) `route_instances` for `pl100@1>2~pl200@2>4` around Monday 08:00 with no delays: exactly one instance (RE 100 + RE 200). The RE 102 start is deduplicated because it leads to the same RE 200;
  - (f) the same with RE 100 `(4, 4, 4)` at B: two instances, RE 102 + RE 200 (07:30 → 09:20) and RE 100 + RE 201 (08:00 → 09:34);
  - (g) `parse_route_key` returns None for `"pl100@1>2~pl200@3>4"` (legs do not connect), for `"x@1>4"`, for a key whose first station is not `from`, and for 6 legs;
  - (g2) `route_instances` for `pl100@1>2~pl250@2>4` (on `make_late_connection_timetable()`) with RE 100 and RE 25 both `(4, 4, 4)` at B: one instance; with RE 25 `(0, 0, 0)`: no instance;
  - (h) within one `plan_routes` call, `FixedForecaster.keys` has no duplicates (each run is forecast at most once per request).

## 7. Responses and endpoints

- [x] 7.1 Implement `src/dbrel/planner/respond.py` with the following, matching the D12 shapes key for key. Verify by the tests in 7.4.
  - `moment(epoch) -> dict`;
  - `forecast_json(arr_row) -> dict | None`;
  - `journey_json(tt, cache, legs, names) -> dict`, where `names(train_id) -> (name, number_name, category)`;
  - `change_json(...)`, applying the D8 rules.
- [x] 7.2 In `src/dbrel/web/queries.py`:
  - add `plan(session, from_id, to_id, at_epoch, date_str, time_str) -> tuple[int, dict]` and `plan_timings(session, from_id, to_id, at_epoch, date_str, time_str, route) -> tuple[int, dict]`. They return the HTTP status and body for D12 validation steps 4–6 and the 200 body. Steps 1–3 and 7 live in the view;
  - add a helper `_planner_forecaster(session, tt) -> ArrivalForecaster | None`, which uses `current_model()` and `current_artifact(ML_MODELS_DIR)` and returns None without a model. Tests monkeypatch it;
  - add a helper `_planner_names(session)`, which returns a cached `names` function using `session.get(Train, id)`, `passenger_name` and `display_name`;
  - rewrite the comment above `_model_cache` to say that the journey planner is the one place where the server runs the model (D1).

  Verify that `uv run pytest -q tests/test_station_board.py tests/test_train_route.py tests/test_browse.py` still passes.
- [x] 7.3 In `src/dbrel/web/views.py`, add `plan(request)` and `plan_timings(request)` with D12 validation steps 1–3 and 7 (the `BoundedSemaphore` with a timeout) and `read_scope()`. In `src/dbrel/web/urls.py`, add the two `path(...)` lines **above** the `re_path` catch-all. Verify that `uv run python manage.py check` (or the project's equivalent; see how `tests/test_station_board.py` sets up Django) reports no issues.
- [x] 7.4 Write `tests/test_planner_api.py`, calling the views through `django.test.RequestFactory` as `tests/test_station_board.py::_moment` does. In **every** test, monkeypatch `planner.timetable.get_timetable` (as imported by `queries`) to return `make_timetable()`, so that no test ever loads a real database's timetable. Also monkeypatch `queries._planner_forecaster` to return a `FixedForecaster` (or None for the 503 test) and `queries._planner_names` to return fixed names. A station is unknown (404) when its id is not in `tt.si_of_id`. Verify that `uv run pytest -q tests/test_planner_api.py` passes. Tests:
  - 400 for a missing `time`, `from == to`, `from=abc`, and a bad route key on timings;
  - 404 for `to=12345`;
  - 503 with `"no delay model is available"` when `_planner_forecaster` returns None;
  - 200 for Monday 08:00 A→D: 3 journeys, the D12 top-level keys exactly, `journeys[0]["legs"][0]["departure"] == {"epoch": …, "date": "2026-10-19", "time": "08:00"}`, `n_changes == 1`, `changes[0]["planned_min"] == 6` (RE 200 starts at B, so `left_min == 6` with no delays);
  - `at_risk` true with RE 100 `(0, 0, 3)` at B (08:30 + 3 + 5 = 08:38 > 08:36) and `left_min == 6`;
  - `reason == "from_unserved"` for from=Z, `"to_unserved"` for to=A (nothing arrives at A), `"no_connection"` for A→C at Monday 10:00, and `"search_limit"` with a monkeypatched tiny budget;
  - timings 200 with `window.from.time == "06:00"` and `window.to.time == "10:00"` for 08:00.

## 8. Warm-up, deployed rebuild and parity

- [x] 8.1 In `src/dbrel/web/wsgi.py`, extend `_warm` so that after `warm_model` it opens `read_scope()` and calls `get_timetable(session)` in its own `try`, logging any exception as now (D13). Verify by starting `DBREL_DB=data/deploy/slim.sqlite uv run dbrel serve --no-frontend` and checking that the log shows the timetable build line within a minute; stop the server afterwards.
- [x] 8.2 Rebuild the slim database with the capped launcher: `uv run dbrel ml launch build-slim --memory 8G -- env PYTHONPATH=src .venv/bin/python scripts/build_slim_db.py --source data/dbrel.sqlite --out data/deploy/slim.sqlite`. Before launching, tell the user it takes several minutes; earlier builds took tens of minutes. Verify that the journal shows the build finished, the `running profile: … rows, window …` line, and the new size, and that `sqlite3 -readonly data/deploy/slim.sqlite "select count(*), min(weekday), max(weekday) from train_variant_weekday"` gives a count > 0 and weekdays 0..6. Then run task 3.5 against the slim file. If the build fails, stop and report; the backup from 0.2 is untouched.
- [x] 8.3 In `src/dbrel/deployed_compare.py`, have `requests_for` add plan requests: Monday 08:00 of the week of `service_date`, for 3 pairs of the sampled stations (first↔second, second↔third, third↔first) plus every pair given with a new `--plan FROM:TO` option of `scripts/compare_deployed_db.py`. Each request returns `queries.plan(...)[1]` with the `stats` key removed. Verify that `uv run pytest -q tests/test_deployed_compare.py` still passes.
- [x] 8.4 Run the comparison: `uv run dbrel ml launch compare-deployed --memory 8G -- env PYTHONPATH=src .venv/bin/python scripts/compare_deployed_db.py --deployed data/deploy/slim.sqlite --plan <Köln Hbf id>:<Berlin Hbf id>`. Get the ids with `sqlite3 -readonly data/deploy/slim.sqlite "select id,name from station where name in ('Köln Hbf','Berlin Hbf')"`. Verify that it reports 0 differences. If there are differences, stop and report them to the user.

## 9. Measurements on the deployed file

- [x] 9.1 Write `scripts/plan_benchmark.py`, which:
  - opens `DB_PATH` with `make_engine`;
  - resolves the station names `Köln Hbf`→`Berlin Hbf`, `München Hbf`→`Hamburg Hbf`, `Aachen Hbf`→`Dortmund Hbf`, `Bochum Hbf`→`Essen Hbf`, `Freiburg(Breisgau) Hbf`→`Kiel Hbf` and `Frankfurt(Main)Hbf`→`Stuttgart Hbf` (exact name first, else `LIKE` on the first word, printing the name used);
  - plans each pair twice (cold, then warm) for next Tuesday at 08:00 by calling `queries.plan`;
  - prints, per pair: status, number of journeys, `reason`, `stats`, seconds, and for journey 1 the route key, departure and expected arrival;
  - prints the index build seconds and `nbytes`, and `VmRSS` + `VmSwap` from `/proc/self/status` before loading, after the index and after all plans.

  Verify that the script runs under `uv run dbrel ml launch plan-bench --memory 3G -- env DBREL_DB=data/deploy/slim.sqlite PYTHONPATH=src .venv/bin/python scripts/plan_benchmark.py` and that its journal shows all 6 pairs.
- [x] 9.2 Write `docs/route-planner-measurements.md` with the table from 9.1, the 3.5 numbers and the date of the run. Check the routes: Köln→Berlin must not route through a station far away, such as via München. Report to the user if any route looks wrong. Verify that the file exists.
- [x] 9.3 **Gate.** Report the measurements to the user. Stop here and wait for their decision if any of these hold: a warm plan takes more than 5 s at the median or 20 s at most, a cold plan takes more than 60 s, the added RSS + swap is more than 350 MB, or any pair answers `search_limit`. Do not tune constants on your own. Verify that the user has answered before group 10 starts.
- [x] 9.4 Write and run `scripts/check_planner_parity.py` on the slim file (capped launcher, 3G). For 50 random `(variant, service_date)` runs from the planner index (seed 0, dates next Monday to Sunday) and every arrival row of each:
  - (a) take the planner's p50 from `ModelForecaster`;
  - (b) take the browser-path input `queries._model_input(session, manifest, train=…, station_id=…, service_date=…, direction=…, holiday_set=…, arr_sec=…, dep_sec=…, variant=path_sha1)` and run it through `onnxruntime.InferenceSession(<artifact>/model.onnx)` with inputs `cat` (int64, [1, 8]) and `num` (float32, [1, 26]), reading p50 from `quantiles[0, 1]`.

  Get the direction from the same `_ModelDirectionCache` class. Verify that the largest |a − b| is ≤ 0.01 and print it (delay-model spec, new requirement).

## 10. Frontend plumbing (`bahn-buddy-predict/src`)

- [x] 10.1 In `lib/api.ts`, add the D12 types (`Moment`, `PlanForecast`, `PlanLeg`, `PlanChange`, `Journey`, `PlanResponse`, `TimingsResponse`, `PlanReason = "from_unserved" | "to_unserved" | "no_connection" | "search_limit"`) and the functions `plan(from, to, date, time)` and `planTimings(from, to, date, time, route)`. Export the `ApiError` class so that the UI can read `status` (a 503 needs its own message). Verify that `npx tsc --noEmit` is clean.
- [x] 10.2 In `hooks/use-dbrel.ts`, add `usePlan(p?: {from: number; to: number; date: string; time: string})` and `usePlanTimings(p?: {…; route: string})`. Both use `enabled: p !== undefined`, a query key that lists every param, `staleTime: 5 * 60_000` and `retry: false`. Give `useSuggest` an optional third parameter `only`, added to its query key and passed to `api.suggest`. Verify that `npx tsc --noEmit` is clean and that `grep -n "useSuggest(" src/components` shows the existing calls unchanged.
- [x] 10.3 Add the D14 strings to both locales in `lib/i18n.ts`. Verify that `npx tsc --noEmit` is clean; the `Copy` type catches a key missing in one locale.
- [x] 10.4 In `components/v2/icons.ts`, add the 7 icon kinds of D14 after checking each name in `node_modules/lucide-react/dist/lucide-react.d.ts` (fall back as D14 says). In `components/v2/run-format.ts`, add `berlinMoment(epoch)` and `formatDuration(min, locale)` ("4 h 34 min" / "4 Std. 34 Min."; under 60 → "34 min" / "34 Min."). In `components/v2/paths.ts`, add `plan(...)`. Verify that `npx tsc --noEmit` is clean, and spot-check `berlinMoment(1792483200)` in `node -e` against `TZ=Europe/Berlin date -d @1792483200`.

## 11. Frontend views

- [x] 11.1 Create `routes/plan.tsx` (D14) rendering `ShellV2` + a placeholder `PlannerV2` that only prints its props, then run `npm run build` so that `routeTree.gen.ts` is regenerated. Verify that `grep -c '"/plan"' src/routeTree.gen.ts` is ≥ 1 and that `npx tsc --noEmit` is clean.
- [x] 11.2 Create `components/v2/StationPicker.tsx` (D14). Verify it in the browser on the local server. Build with `npm run build`, then run `DBREL_DB=data/deploy/slim.sqlite uv run dbrel serve` and open http://127.0.0.1:8000/plan. Check that typing "Dortm" lists only stations; that the list closes on an outside click; that choosing one fills the field; and that typing again clears the choice.
- [x] 11.3 Build the `PlannerV2` form (D14 state model and layout): From, swap, To, date, time, Today/Now, and the submit button with its gate. Verify in the browser that:
  - the button stays disabled until both stations, the date and the time are set, and stays disabled when From equals To;
  - no `/api/plan` request is sent before pressing (network panel);
  - pressing navigates to `/plan?from=…&to=…&date=…&time=…`.
- [x] 11.4 Create `components/v2/JourneyCard.tsx` and render the plan results in `PlannerV2`, including the loading, error, 503 and empty-reason states (D14). Mark each card with `data-journey`. Verify in the browser:
  - Köln Hbf → Berlin Hbf next Tuesday 08:00 shows up to 3 cards in the response's order;
  - editing the time hides them;
  - reloading the URL restores the inputs and the results;
  - a server without a model (`ML_MODELS_DIR` pointed at an empty directory) shows `t.noModel`.

  Then measure `[data-journey]` heights at 1280 px and 390 px: they are all equal.
- [x] 11.5 Create `components/v2/JourneyTimings.tsx`: the D14 timings window and Earlier/Later, highlighting the clicked instance, `data-timing` on each row, and a fixed `RUN_ROW_HEIGHT`. Wire the card click to `option` in the URL. Verify in the browser:
  - clicking card 2 marks it (`aria-pressed`) and shows its times, with the clicked one highlighted;
  - Later requests `time` 4 h later (network panel);
  - reloading with `&option=1` restores the choice;
  - `[data-timing]` heights are equal for a normal, a risky and an after-midnight row at 1280 px and 390 px. If no risky row occurs naturally, force one in DevTools by editing the response.
- [x] 11.6 Create `components/v2/JourneyLegs.tsx` for the chosen instance (D14). Verify in the browser that each leg shows its train, stations, times and `DelayText`; that the change rows show the minutes left and the risk tag; and that clicking a leg's train opens `/train/<id>?station=<boarding station id>` with that stop highlighted, and Back returns to the same results.
- [x] 11.7 Add the header link to `ShellV2.tsx` (D14). Verify that it appears on `/`, `/train/…`, `/line/…` and `/plan`; that it has `aria-current="page"` only on `/plan`; that at 360 px the logo, the icon and both toggles stay on one row; and that `/v1` is unchanged (no link there).
- [x] 11.8 Run `npm run lint`, `npx tsc --noEmit` and `npm run build` in `bahn-buddy-predict`. Verify that all three pass with no new warnings in the touched files.

## 12. Integration check

- [x] 12.1 Run the full backend suite `uv run pytest -q` and verify that it passes. Report the counts of passed and failed tests.
- [x] 12.2 On the local server (`DBREL_DB=data/deploy/slim.sqlite uv run dbrel serve`, **not** Fly), walk through every scenario of `specs/route-planner/spec.md` and the changed scenarios of `specs/web-frontend-v2/spec.md`. Do it in DE and EN, light and dark, at 1280 px and 390 px. Check `/v1` and its pages too. Verify by writing a pass/fail line per scenario into the report to the user.
- [x] 12.3 Report to the user: what was built; the test results; the measurements (`docs/route-planner-measurements.md`); the parity results (8.4, 9.4); the new slim DB size; and the backup file name. **Do not commit or push**; wait for the user's instruction.

## 13. Readability revision (D16)

- [x] 13.1 In `planner/respond.py`, add `miss_chance` and `worst_miss_chance` (D8) and the `miss_chance` key on changes and journeys; derive `at_risk` from the band. Add the key to the shape assertions in `tests/test_planner_api.py` and band tests to `tests/test_planner_forecast.py`. Verify that `uv run pytest -q tests/test_planner_*.py` passes.
- [x] 13.2 Add `MissChance` and the `miss_chance` fields to `lib/api.ts`, the D16 strings to `lib/i18n.ts`, and `usePlanTimingWindows` in place of `usePlanTimings`. Verify that `npx tsc --noEmit` is clean.
- [x] 13.3 Rebuild `JourneyRow` with `JourneyColumnHeader` and use it in both lists; add `MissChanceTag` to `PlannerForecast.tsx` and `delayLabel` to `planner-format.ts`. Verify in the browser at 1280 px and 390 px, DE and EN, that the headers and bands are not cut off.
- [x] 13.4 Rewrite `JourneyTimings` as D16 (5 fixed slots, windows fetched ahead, no remount) and pass `resetScroll: false` when choosing a connection. Verify in the browser that clicking another connection keeps `scrollY` and the card's top and height unchanged while it reloads, and that Later shows the next 5 runs.
- [x] 13.5 Rewrite `JourneyLegs` as the D16 timeline. Verify in the browser that each arrival shows its forecast and delay, and each change its planned and expected minutes and its band.
- [x] 13.6 Move Today/Now under the date/time fields; add `lib/frequent-stations.ts` and wire it into `PlannerV2` (record on submit) and `StationPicker`. Verify in the browser that after a search an empty field lists both stations under "Häufig genutzt", and that typing "Bo" puts Bochum Hbf first.
- [x] 13.7 Prefetch the first other-times window of all listed connections (`usePrefetchPlanTimings`). Verify in the browser that all three routes are requested before any click and that clicking the third shows its entries at once.
- [x] 13.8 Run `npm run lint`, `npx tsc --noEmit` and `npm run build`; verify all pass with no new warnings in the touched files.

## Workflow follow-up

- After the user approves, they decide how and when to replace the database on the Fly volume (`/data/dbrel.sqlite`) with the rebuilt `data/deploy/slim.sqlite`. The new server code needs the new table to plan; without it the planner answers with `from_unserved` and every other page keeps working.
- Commit and push only when the user asks; a push to main deploys.
- Archive this change (`openspec archive add-route-planner`) after the user confirms it works on Fly.
