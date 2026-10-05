# Tasks

Before every task, tell the user its current duration estimate and the basis for it. Keep
hands-on work separate from unattended computation. Groups 1 and 2 use fixtures only. Group 3
reads the real collection database, and each step there needs the user's go-ahead. Every
real-data step runs as a capped job (`dbrel ml launch ... --memory 4G`) and reports peak RSS plus
swap. Nothing in this change writes to the collection database.

## 1. Route-variant run summary (server side)

- [x] 1.1 Add a helper in `web/queries.py` that returns, for given trains, each variant's
  `(last_service_date, n_runs)`. It reads `train_variant_run` when the database has it (checked
  once per engine) and aggregates `trip` otherwise. Route `train_route`'s latest-variant choice and
  `_variant_per_train_at`'s ranking through it (design D4). Verify with a test on one fixture, run
  once with only `trip` and once with only the summary. It must give the same chosen variant:
  - for a train whose latest run used a short variant while another variant ran more often
  - for a train with an old variant
  - for a NULL variant Verify also that `tests/test_train_route.py` and the board tests still pass.
  Est.: 1.5 h.

## 2. Pattern storage in the deployed copy

- [x] 2.1 Add the pattern encoding: from one variant's rows in stop-sequence order, its start time,
  its pattern tuple (design D2) and its id runs (D3), and the inverse. Verify with unit tests:
  - two variants an hour apart share a pattern
  - a platform difference gives two patterns
  - a 25:10 arrival round-trips as 90,600
  - a missing arrival stays missing
  - non-consecutive ids round-trip exactly
  - a variant with no times at all round-trips

  Est.: 1.5 h.
- [x] 2.2 Extend `scripts/build_slim_db.py`:
  - create `stop_pattern`, `pattern_stop`, `train_variant`, `variant_id_run`, the
    `station_stop` view and `train_variant_run` instead of copying `station_stop` and `trip`
  - stream the source schedule once to fill them
  - verify every rebuilt row, id included, against the source
  - write to `<out>.partial` and rename only on success
  - print size and counts (design D1, D5)

  Verify with a test that builds a copy of the ML fixture database:
  - the view's rows equal the source's in every field, with counts per feed
  - deliberately corrupting one pattern stop makes the build fail, name the train and stop,
    and leave an existing output file unchanged
  - the output lists size, rows, patterns, pattern stops and summary rows

  Est.: 2.5 h.
- [x] 2.3 Add `scripts/compare_deployed_db.py` (design D6). It calls these on two engines and
  reports any difference by request and JSON path:
  - `station_board` (fixed `now`)
  - `train_route` with and without `station_id`
  - `line_directions`, `line_stations` and `line_detail`
  - `trips_around` and `corridor_trips_around` (both directions, a few stops and times)
  - `suggest`

  Verify with a test that a pattern-built copy of the train-route fixture gives identical
  responses for every call, the earlier and later runs included. Verify also that a copy with one
  changed platform is reported with its path. Est.: 1.5–2 h.
- [x] 2.4 Update the README's "Deploying" section: what the copy holds now (patterns, view, run
  summary over all runs), the build's verification and size report, and the parity check command.
  Verify that every documented command runs as written on the fixture. Est.: 20 min.

## 3. Real data (each step needs the user's go-ahead)

- [x] 3.1 Build the real copy to `data/deploy/slim-patterns.sqlite` as
  `dbrel ml launch build-slim --memory 4G -- ... scripts/build_slim_db.py --out ...`. Verify that
  the build's own verification passes, and record in this file the size (against today's
  1,693 MiB), the counts, the time and the peak RSS plus swap. Est.: 10 min hands-on plus about
  10 min unattended (today's build takes 2.5 min; this one adds two streaming passes).
  Result, 2026-10-05: 518 MiB (543,035,392 bytes) against 1,693 MiB, −69%, in 234 s. The view
  presents 5,536,057 schedule rows from 213,814 patterns (1,782,323 pattern stops; 742,134
  variants), and the verification matched every row, id included. The run summary has 502,775 rows
  over all runs, and the notice totals have 166,161 rows, with the fingerprint matching the model.
  1,088,951 other rows were copied from 11 tables. Peak RSS was 584 MiB with 1 MiB of swap; the
  group peak reached the 4 GiB cap from page cache but the job was not killed.
- [x] 3.2 Measure board latency on the three busiest stations (Düsseldorf Hbf, Berlin Ostkreuz,
  Köln Hbf) and the route of NX 26405 from Wuppertal Hbf, on the old and new copies (warm, median
  of five). Verify each board is under 0.5 s. If not, add the covering index from the design's
  risk list and measure again. If still not, stop and report before taking the new-ids fallback
  (design D3 alternative), since that changes the parity requirement. Est.: 30 min.
  Result, 2026-10-05: real board times are 1.2–16.2 s on both copies (Düsseldorf 4.6 s, Berlin
  Ostkreuz 16.2 s, Köln 1.2 s). 98% of that is the known direction bug, outside this change
  (design: Risks). With directions cached, the old and new copies compare as:

  | Board / route | Old | New |
  |---|---|---|
  | Düsseldorf Hbf | 0.29 s | 0.36 s |
  | Berlin Ostkreuz | 0.13 s | 0.14 s |
  | Köln Hbf | 0.49 s | 0.61 s |
  | NX 26405 from Wuppertal | 0.038 s | 0.038 s |

  The first build was slower (0.41–0.84 s). The user approved `ANALYZE` in the build (design D5)
  and an id tie-break in the board (D7, after the same data gave 46 and 47 calls on two copies),
  and accepted Köln at about 0.6 s in place of the 0.5 s target. The covering index was tried and
  left out (+44 MB for 0.02 s). The rebuild took 132 s with a peak RSS of 566 MiB and is 518 MiB.
- [x] 3.3 Run `scripts/compare_deployed_db.py` between the collection database and the new copy,
  as a capped job. Include the Aachen–Dortmund RE4 at Wuppertal Hbf and the Schwerin–Szczecin RE4,
  which differed on the 60-day copy. Verify zero differences, and record the sample sizes and run
  time here.
  Est.: 15 min hands-on plus about 10 min unattended.
  Result, 2026-10-05: 0 differences in 497 requests, in 300 s with a peak RSS of 1,238 MiB. The
  sample was 25 stations (the 3 busiest, Wuppertal Hbf, Malchin and random ones), 26 trains (the 5
  with the most route variants, NX 26405 and random ones), 12 lines (both RE4s and random ones),
  5 line-less trains and 8 search terms. The first run found 13 differences, all in one request:
  two runs at the same minute at Malchin were ordered by the query plan. The user approved the
  tie-break fix (design D7) and the rerun was clean.
- [x] 3.4 After group 4: run the Docker image against the new copy (`slim-compact.sqlite`) with a 512 MB limit, as on 2026-10-05. Request
  the search, a train route, a board and the model file. Verify all return 200, record the
  memory, and with the user's approval replace `data/deploy/slim.sqlite` with the new copy.
  Est.: 15 min.
  Result, 2026-10-05: in the rebuilt image under a 512 MB limit, these all returned 200:
  - the page, search, the NX 26405 route from Wuppertal (15 stops, Wuppertal included)
  - the Köln board (1.3 s) and the Berlin Ostkreuz board (15.2 s; the direction bug)
  - the RE4 runs around Wuppertal and the 15.2 MB model file

  Memory was 146–149 MiB with no OOM. The user approved replacing the copy but asked to keep the
  older files as backups. So `slim.sqlite` is now the 260 MiB copy (its WAL folded back, journal
  mode DELETE). The previous files are `slim-backup-1693MiB-before-patterns.sqlite` and
  `slim-backup-518MiB-patterns-only.sqlite`. The server code reads both formats, so either works
  as a rollback.

## 4. Further lossless compaction (design D8; the user asked for it before 3.4)

- [x] 4.1 Implement A–F in `dbrel.deployed` and the builder:
  - `variant_path` and `place_name`
  - `train_data` behind a `train` view
  - `WITHOUT ROWID` key tables
  - `variant_run_data` behind a `train_variant_run` view
  - no `stop_pattern`
  - only the indexes the server uses

  The build also verifies every `train` row and every run-summary row against the source. Verify
  with the fixture tests:
  - the `train` view equals the source `train` in every column
  - the run-summary view equals the source aggregate
  - the schedule check, the corruption check and `tests/test_deployed_runs.py` still pass
  - `scripts/compare_deployed_db.py` finds 0 differences on the fixture

  Est.: 2–3 h.
- [x] 4.2 Rebuild the real copy (capped, 4G) and record its size against 518 MiB, then redo the
  3.2 board timings (directions cached) and the 3.3 parity check on it. Verify that the build's
  checks pass, that there are 0 parity differences, and that no board is more than 0.1 s slower
  than on the 518 MiB copy. Record the results here. Est.: 10 min hands-on plus about 15 min
  unattended.
  Result, 2026-10-05: `data/deploy/slim-compact.sqlite` is 260 MiB (518 MiB before group 4, 1,693
  MiB before the change). It took 339 s with a peak RSS of 584 MiB. The build checked 5,536,057
  schedule rows, 336,736 trains (every column) and 502,775 run-summary rows against the collection
  database. The lookups hold 111,758 route hashes, 35,563 route paths and 4,062 place names.
  Directions cached, against the 518 MiB copy:

  | Board / request | 518 MiB | 260 MiB |
  |---|---|---|
  | Düsseldorf Hbf | 0.36 s | 0.44 s |
  | Berlin Ostkreuz | 0.147 s | 0.149 s |
  | Köln Hbf | 0.63 s | 0.68 s |
  | NX 26405 route | 0.040 s | 0.039 s |
  | RE4 around Wuppertal | 0.012 s | 0.012 s |

  That is at most 80 ms slower. The parity check on the same sample as 3.3 found 0 differences in
  497 requests (371 s, peak RSS 1,157 MiB).

