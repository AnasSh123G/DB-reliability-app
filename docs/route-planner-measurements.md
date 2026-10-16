# Route planner measurements

Run dates: 2026-10-14–15. Local validation only; nothing deployed.

Final gate passes: warm median **4.29 s**, warm/cold maxima **11.92 / 15.57 s**,
added RSS + swap **349.27 MiB** (narrow margin). **947 backend tests pass**;
source/deployed parity has **0 differences across 368 requests**.
Browser/model parity has **0.0 minutes difference across 511 arrivals**.
The latest results and scenario evidence appear below; early failed measurements
are retained to explain the optimization work.

## Collection timetable smoke test

Executed through `dbrel ml launch planner-index --memory 6G` against
`data/dbrel.sqlite`. Successful exit, no swap.

| Metric | Result |
| --- | ---: |
| Stop rows | 833,097 |
| Route variants | 92,201 |
| Stations | 9,699 |
| Indexed departures | 739,101 |
| Array storage | 24,831,044 bytes |
| Build time | 34.36 s |
| Process peak RSS | 525,192 KiB |
| RSS after build | 456,312 KiB |
| Swap after build | 0 KiB |

These are collection-database figures, not the deployed-memory gate results.
Log: `data/ml/logs/planner-index-20261014T203128Z.log`.

## Deployed validation

The capped rebuild succeeded in 303 seconds. New file size: **190,210,048 bytes**
(181.40 MiB). Backup: `data/deploy/slim-backup-184MiB-before-running-profile.sqlite`,
unchanged at 184,340,480 bytes.

The rebuilt database has 385,206 weekday-profile rows spanning weekdays 0–6,
with window 2026-09-01 through 2026-09-28. The build verified all 4,130,487
schedule rows against the source and copied 166,161 notice-rate totals matching
the current model fingerprint.

The slim-index smoke test, capped at 3 GiB, produced the same counts and array
size as the collection test: 833,097 stops, 92,201 variants, 9,699 stations,
739,101 departures and 24,831,044 bytes of arrays. Build time: **5.09 s**.
Process peak RSS: 412,864 KiB; final RSS + swap: 345,224 + 0 KiB.
Log: `data/ml/logs/planner-index-20261014T203916Z.log`.

Source/deployed response parity passed: **368 requests, 0 differences**, in
345 seconds. This includes both directions for three sampled station pairs and
Köln Hbf ↔ Berlin Hbf. Search statistics were excluded as specified.
Log: `data/ml/logs/compare-deployed-20261014T203928Z.log`.

The local no-frontend server warmed the slim index successfully and logged its
build (5.05 seconds) within one minute; the server was then stopped.

## Six-pair benchmark

Executed with a 3 GiB cap on the rebuilt slim file, for Tuesday 2026-10-20 at
08:00 Europe/Berlin. The benchmark ran after the comparison finished, with the
local server stopped. Index construction in this process took 5.516 seconds.

| Pair | Phase | Seconds | Journeys | Reason | Searches | Runs forecast |
| --- | --- | ---: | ---: | --- | ---: | ---: |
| Köln Hbf → Berlin Hbf | cold | 8.322 | 0 | search_limit | 1 | 226 |
| Köln Hbf → Berlin Hbf | warm | 8.224 | 0 | search_limit | 1 | 271 |
| München Hbf → Hamburg Hbf | cold | 8.144 | 0 | search_limit | 1 | 348 |
| München Hbf → Hamburg Hbf | warm | 8.229 | 0 | search_limit | 1 | 416 |
| Aachen, Hbf → Dortmund Hbf | cold | 20.114 | 3 | — | 5 | 491 |
| Aachen, Hbf → Dortmund Hbf | warm | 20.025 | 3 | — | 10 | 727 |
| Bochum Hbf → Essen Hbf | cold | 0.119 | 3 | — | 3 | 35 |
| Bochum Hbf → Essen Hbf | warm | 0.016 | 3 | — | 3 | 35 |
| Freiburg(Breisgau) Hbf → Kiel Hbf | cold | 8.119 | 0 | search_limit | 1 | 550 |
| Freiburg(Breisgau) Hbf → Kiel Hbf | warm | 8.218 | 0 | search_limit | 1 | 696 |
| Frankfurt (Main) Hauptbahnhof → Stuttgart Hbf | cold | 8.175 | 0 | search_limit | 1 | 285 |
| Frankfurt (Main) Hauptbahnhof → Stuttgart Hbf | warm | 8.504 | 0 | search_limit | 1 | 286 |

All requests returned HTTP 200. A search-limit response is an incomplete search,
not evidence that no usable journey exists.

Successful first journeys:
- Aachen, Hbf → Dortmund Hbf (cold): 08:31 → 12:36, `pl86550@2>4259~pl86550@4259>1999~pl87007@1999>269~cIC@269>109`.
- Aachen, Hbf → Dortmund Hbf (warm): 08:31 → 12:36, `pl86550@2>4259~pl86550@4259>1999~pl87007@1999>269~cIC@269>109`.
- Bochum Hbf → Essen Hbf (cold): 08:02 → 08:23, `pl86607@43>128`.
- Bochum Hbf → Essen Hbf (warm): 08:02 → 08:23, `pl86607@43>128`.

Route review: the Aachen result runs via Langerwehe, Düren and Köln; the Bochum
result is direct to Essen. Neither makes a geographically distant detour.
Köln → Berlin and the other three search-limited pairs have no returned route to
inspect; their route plausibility is **unverified**.

| Memory stage | RSS KiB | Swap KiB | RSS + swap MiB |
| --- | ---: | ---: | ---: |
| before_loading | 61,348 | 0 | 59.91 |
| after_index | 342,564 | 0 | 334.54 |
| after_all_plans | 551,000 | 0 | 538.09 |

Added RSS plus swap after all plans: **478.18 MiB**.
This is the process RSS/swap metric required by the gate; the systemd cgroup
memory figure is not substituted for it.

## Initial performance gate: failed (2026-10-14)

- Warm median **8.227 s** exceeds 5 s.
- Warm maximum **20.025 s** exceeds 20 s.
- Cold maximum **20.114 s** is below 60 s.
- Added RSS plus swap **478.18 MiB** exceeds 350 MiB.
- Four of six pairs answer `search_limit` in both phases.

The user subsequently authorized profiling and optimization. No constants were
relaxed. Frontend tasks remain gated on the optimized measurements below.

Benchmark log: `data/ml/logs/plan-bench-20261014T204530Z.log`.


## Test environment

The default `uv run pytest -q` cannot collect the existing
`tests/test_recency_weights.py` because PyTorch is absent from `.venv`.
Using the repository's existing PyTorch packages succeeds:

```sh
PYTHONPATH=src:scripts:data/build/torch-rocm/venv/lib/python3.13/site-packages uv run pytest -q
```

**947 passed, 0 failed**, with six warnings from existing ONNX export tests.
No dependencies were installed or changed. Planner-module line coverage from
stdlib trace is 95–100%; 119 focused planner/deployed tests pass, plus two
benchmark station-resolution regression tests added afterward.

## Disk cleanup authorized on 2026-10-14

`data/` occupied approximately 200 GiB. Removed only redownloadable archive
partitions and regenerable probe/compiler outputs after checking for active
users and source references.

| Removed path | File bytes | GiB |
| --- | ---: | ---: |
| `data/archive/raw_data` | 53,451,032,632 | 49.78 |
| `data/ml/full_year/probe` | 2,916,575,690 | 2.72 |
| `data/build/xgboost-rocm/src-release-3.2.0/build` | 481,867,728 | 0.45 |
| `data/build/xgboost-rocm/src-release-3.2.0/lib` | 420,495,904 | 0.39 |

Removed file sizes total **53.34 GiB**.
`du -sh data` is now approximately **147 GiB**. Filesystem available space
increased from about 177 GiB to 228 GiB. Shared/COW allocation and rounding
mean file sizes need not equal the change in filesystem free space.

Kept the 48 GiB collection database, all database snapshots and protected
`slim-backup-*` files, deployed database, trained artifacts/checkpoints, current
training inputs, and the 13 GiB PyTorch environment used by tests. The archive
manifest and replay state remain; future raw replays may redownload partitions.
No prepared-data symlink was followed or removed.

The user approved profiling and optimizing the failed planner gate before
frontend work; search limits remain unchanged.

## Performance work resumed on 2026-10-15

User approved profiling/optimization before frontend implementation. No search
limits have been increased. Implemented exact scheduled-bound/window caches,
proof-reviewed same-arrival state dominance, planner-only single-thread ONNX
inference, and reclamation of unused loader/search buffers. The initial zero-leg
source state remains separate from return-to-source transfers because only the
transfer window includes the carried-delay lookback.

An isolated memory experiment (`plan-memory-release-20261015-20261015T012406Z.log`)
showed index RSS decreasing from 347,672 to 287,996 KiB when freed native
buffers were returned to the OS. After a successful Aachen plan, reclamation
reduced 372,600 to 354,932 KiB. SQLite `shrink_memory` saved only 72 KiB, so no
SQLite cache or mmap settings were changed.

Single-thread inference plus the earlier state optimization reduced Aachen's
warm time to 1.27 s from the original 20.03 s. That intermediate run still had
search-limited long journeys and exceeded the memory gate. These intermediate results did not pass the gate; the final results below do.

## Browser/model numerical parity after optimization

Passed with model `20261014T114610Z`, seed 0: **50 runs, 511 usable arrival
rows**, maximum absolute median difference **0.000000 minutes** (tolerance
0.01). Seven arrival rows beyond faulty schedule cuts have no planner forecast
and were excluded explicitly. The browser path used the existing `_model_input`
encoder and independent ONNX inference.
Log: `data/ml/logs/planner-browser-parity-20261015-20261015T013001Z.log`.


## Indexed-departure benchmark (2026-10-15)

All twelve requests returned three journeys with no `search_limit`.

| Pair | Cold seconds | Warm seconds |
| --- | ---: | ---: |
| Köln → Berlin | 10.456 | 5.720 |
| München → Hamburg | 17.391 | 13.599 |
| Aachen → Dortmund | 0.839 | 0.496 |
| Bochum → Essen | 0.075 | 0.026 |
| Freiburg → Kiel | 14.948 | 12.412 |
| Frankfurt → Stuttgart | 5.164 | 4.531 |

Warm median **5.125 s** still narrowly exceeds 5 s. Warm maximum **13.599 s**
and cold maximum **17.391 s** pass. Final RSS plus swap **426,612 KiB** minus
baseline **61,264 KiB** gives **356.785 MiB** added, still above 350 MiB.
The gate remains open while exact-semantics optimization continues.
Log: `data/ml/logs/plan-bench-indexed-20261015-20261015T013558Z.log`.

Numerical parity was repeated after tuple batch encoding: **511 arrivals over
50 runs, maximum difference 0.0 minutes**, passed.
Log: `data/ml/logs/planner-browser-parity-final-20261015-20261015T013916Z.log`.


## Cached-delay benchmark (2026-10-15)

All twelve requests returned three journeys with no search-limit result.
Warm median **4.369 s**, warm maximum **12.027 s**, and cold maximum **15.555 s**
pass the latency gates. Final RSS plus swap **421,936 KiB**, baseline **61,260 KiB**:
added memory **352.223 MiB** remains 2.223 MiB above the gate. Identical route
structures are being shared in the direction cache before the final rerun.
Log: `data/ml/logs/plan-bench-delays-20261015-20261015T014020Z.log`.


## Final performance gate: passed (2026-10-15)

| Pair | Cold seconds | Warm seconds |
| --- | ---: | ---: |
| Köln Hbf → Berlin Hbf | 9.562 | 4.777 |
| München Hbf → Hamburg Hbf | 15.568 | 11.919 |
| Aachen, Hbf → Dortmund Hbf | 0.764 | 0.423 |
| Bochum Hbf → Essen Hbf | 0.069 | 0.021 |
| Freiburg(Breisgau) Hbf → Kiel Hbf | 13.559 | 10.748 |
| Frankfurt (Main) Hauptbahnhof → Stuttgart Hbf | 4.445 | 3.810 |

Warm median **4.293 s**, maximum warm **11.919 s**, maximum cold **15.568 s**.
Every request returned three journeys, with no search-limit reason. Köln → Berlin's
first route is direct ICE 555, 08:44 → 13:24 expected; no geographic detour.

Index: **4.232 s**, **24,831,044 array bytes**, RSS **288,780 KiB**.
Baseline RSS + swap **61,268 KiB**, final **418,920 KiB**, swap zero:
added **349.270 MiB**. This passes the 350 MiB gate with a narrow margin.
No search constants were increased. Frontend implementation may proceed under
the user's existing authorization to optimize and continue.

Log: `data/ml/logs/plan-bench-final-20261015-20261015T014403Z.log`.


## Final database and frontend checks

Source/deployed comparison repeated after the performance work: **368 requests,
0 differences in 316 seconds**, including bidirectional Köln/Berlin plans.
Log: `data/ml/logs/compare-deployed-final-20261015-20261015T014554Z.log`.

Frontend TypeScript and production build pass. Whole-app ESLint passes with zero
errors and seven existing Fast Refresh warnings. Forty pre-existing formatting
errors were corrected with Prettier in five legacy components; their behavior is
unchanged. No new warning was introduced in the planner files.


## Browser integration validation (2026-10-15)

**283 checks passed, zero failed** in headless Chromium. Interactions and states
were repeated in DE/EN, light/dark, at 1280/390 px. Deterministic API fixtures
included normal, risky and after-midnight journeys. Every connection entry was
**92 px** and every timing entry **68 px**, with no horizontal overflow. The
360 px header fits on one row. Fresh date/time inputs are empty, and all four
required fields gate submission.

The live slim-backed server independently returned three Köln Hbf → Berlin Hbf
connections for 2026-10-20 08:00. The first is ICE 555, departure 08:44,
scheduled arrival 12:55, forecast arrival 13:24 (+29 minutes). Selecting the
second route loaded its timing instance. A separate server with
`DBREL_MODELS_DIR=/tmp/dbrel-empty-models` returned the actual 503 no-model
response. Validation requests verified missing time, invalid time, identical
stations and unknown station responses.

Fixture-based browser results: `/tmp/dbrel-planner-ui-report.json`;
runner: `/tmp/dbrel-planner-ui.mjs`. Local screenshots:
`/tmp/planner-{de,en}-{light,dark}-{1280,390}.png` and
`/tmp/planner-live-1280.png`.

The scenario table distinguishes controlled backend fixtures from browser
checks and live data; it does not claim that synthetic forecast edge cases
occurred naturally in the collected timetable.

| Route-planner scenario | Result | Evidence |
| --- | --- | --- |
| A valid request | PASS | Live HTTP checks and planner API regression tests |
| Time missing | PASS | Live HTTP checks and planner API regression tests |
| Same station twice | PASS | Live HTTP checks and planner API regression tests |
| Unknown station | PASS | Live HTTP checks and planner API regression tests |
| A late train | PASS | Planner search/routes/API fixture tests |
| An early forecast | PASS | Planner search/routes/API fixture tests |
| The forecast reverses the timetable's order | PASS | Planner search/routes/API fixture tests |
| A change made impossible by a forecast delay | PASS | Planner search/routes/API fixture tests |
| A change that still works | PASS | Planner search/routes/API fixture tests |
| Boarding at the start | PASS | Planner search/routes/API fixture tests |
| A late connecting train can be caught | PASS | Planner search/routes/API fixture tests |
| A punctual connecting train is not waited for | PASS | Planner search/routes/API fixture tests |
| Carried departure delay is capped | PASS | Planner search/routes/API fixture tests |
| A weekday-only train on a Sunday | PASS | Timetable/profile/search fixture tests |
| A holiday counts as Sunday | PASS | Timetable/profile/search fixture tests |
| A stale route variant | PASS | Timetable/profile/search fixture tests |
| A time that jumps a day | PASS | Timetable/profile/search fixture tests |
| Night gap at the start | PASS | Planner search/routes/API fixture tests |
| Three different routes | PASS | Live UI/benchmark plus route/API fixture tests |
| Same route, different train | PASS | Planner search/routes/API fixture tests |
| Only one way exists | PASS | Planner search/routes/API fixture tests |
| An unneeded early start | PASS | Planner search/routes/API fixture tests |
| A change at risk | PASS | Planner search/routes/API fixture tests |
| A comfortable change | PASS | Planner search/routes/API fixture tests |
| Data age is shown | PASS | Planner search/routes/API fixture tests |
| No model | PASS | Live HTTP checks and planner API regression tests |
| A station without trains | PASS | Planner search/routes/API fixture tests |
| A search that runs too long | PASS | Planner search/routes/API fixture tests |
| Earlier and later times of one route | PASS | Live UI/benchmark plus route/API fixture tests |
| A malformed route key | PASS | Planner search/routes/API fixture tests |
| Two first trains lead to the same connection | PASS | Planner search/routes/API fixture tests |
| The button waits for every input | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| Station pickers list stations only | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| Editing withdraws results | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| A connection with a risky change | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| No connection | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| Choosing a connection | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| Later times | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| Following a leg's train | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| Heights are equal | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| Reloading the page | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| Slow search | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |
| Server error | PASS | Chromium interaction/state matrix (8 locale/theme/width combinations) |


| Changed frontend-v2 scenario | Result | Evidence |
| --- | --- | --- |
| Each view is served at the root | PASS | Header/page matrix and Chromium interaction checks |
| An unknown id is handled as in the current interface | PASS | Protected v2 view/v1 route files unchanged; legacy components only reformatted; backend parity and existing regressions pass |
| Requests are identical | PASS | Protected v2 view/v1 route files unchanged; legacy components only reformatted; backend parity and existing regressions pass |
| Behaviour is identical | PASS | Protected v2 view/v1 route files unchanged; legacy components only reformatted; backend parity and existing regressions pass |
| The listed differences are the only ones | PASS | Protected v2 view/v1 route files unchanged; legacy components only reformatted; backend parity and existing regressions pass |
| The planner exists only in the second interface | PASS | Header/page matrix and Chromium interaction checks |
| Opening the planner from a train page | PASS | Header/page matrix and Chromium interaction checks |
| Phone width | PASS | Header/page matrix and Chromium interaction checks |

Header/page matrix: **80 checks passed**, zero failures, covering every v2 view
and `/v1`, train, station, line and unknown planner addresses in all eight
locale/theme/width combinations. Report: `/tmp/dbrel-planner-header-report.json`.

Final review found no remaining actionable defects after correcting the fresh
form defaults and busy/no-model error distinction. No commit, push or deployment
was performed. All protected slim backups remain in place.
