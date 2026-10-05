# Design

## Context

See proposal.md (Why) for the measured sizes. The current state that shapes the approach:

- **The deployed copy is built by `scripts/build_slim_db.py`.** It copies twelve tables whole
  from the collection database, keeps the last 60 days of `trip` as four columns, writes the
  notice-rate totals, and runs `VACUUM`. The result is 1,693 MiB.
- **The server reads the schedule only through the `StationStop` ORM model** (table name
  `station_stop`), in `web/queries.py` and `ml/timetable.py`. It reads `train_id`, `path_sha1`,
  `station_id`, `feed_version_id`, `stop_sequence`, the two planned times, `platform_code` and
  `id`. The server never writes: the web path uses `read_scope()`, which always rolls back, and it
  never calls `create_all`.
- **The server reads `trip` in exactly two places.** `train_route` takes the variant of the most
  recent run, ordered by `(service_date desc, id desc)` over runs with a variant.
  `_variant_per_train_at` ranks a train's variants at a station by `(max(service_date),
  count(*))`, grouped by variant, NULL variant included. `trip` is unique on
  `(train_id, service_date)`, so a train has at most one run per date, and the `id` in that
  ordering never decides anything.
- **Feeds.** The three feed versions are three sources, all in use: GTFS long-distance (1), GTFS
  regional (2) and the IRIS timetable (3). The GTFS feeds' rows have no `path_sha1`, and a GTFS
  train is already one pattern.
- **Row ids carry history.** A train's variants were written at different times, so their ids
  interleave. 167 k of the 742 k variants hold non-consecutive ids. Ordered by stop sequence,
  the 5.54 M rows form 965,426 runs of consecutive ids. `train_route` orders by `id` when no
  variant is known (a train with no recorded run), so ids decide which row wins there.
- **Busiest station:** Düsseldorf Hbf has 22,449 schedule rows, which is what one board query
  filters through.

## Goals / Non-Goals

**Goals:**
- Lossless: the `station_stop` rows the server reads, ids included, and the route-variant choice
  are identical to the collection database's (spec: "Every schedule row is reproduced exactly",
  "Route-variant history covers every recorded run").
- No change to the server's schedule queries. The only server change is the two `trip` reads.
- Board latency on the busiest station stays at a level a person does not notice. The target
  is under 0.5 s, against 0.06 s on today's copy.

**Non-Goals:**
- Changing the collection database or its writers.
- Day-type templates (see proposal).
- Shrinking `train`, `search_alias` or the notice totals.
- Writing to the deployed copy at runtime. It is rebuilt, never updated in place.

## Decisions

### D1. Three tables and a view replace `station_stop`

- `stop_pattern(id, n_stops, digest)`. `digest` is the SHA-1 of the pattern's canonical text,
  used only while building.
- `pattern_stop(pattern_id, position, stop_sequence, station_id, source_stop_id, platform_code,
  arr_offset, dep_offset, pickup_type, drop_off_type)`. The primary key is
  `(pattern_id, position)`, where `position` is 0..n−1 in stop-sequence order. There is an index
  on `(station_id, pattern_id)` for boards.
- `train_variant(id, feed_version_id, train_id, path_sha1, pattern_id, start_sec)`. It is unique
  on `(feed_version_id, train_id, path_sha1)`, with indexes on `train_id` and on
  `(pattern_id, feed_version_id)`.
- `variant_id_run(variant_id, from_position, first_id)`, with primary key
  `(variant_id, from_position)`: one row per run of consecutive row ids (D3).
- The view `station_stop` (id, feed_version_id, train_id, stop_sequence, station_id,
  source_stop_id, platform_code, planned_arrival_sec, planned_departure_sec, pickup_type,
  drop_off_type, path_sha1) is a join of `train_variant` and `pattern_stop`. The times are
  `start_sec + offset`, and NULL stays NULL.

`StationStop` keeps mapping to `station_stop`. SQLite and SQLAlchemy read a view like a table, so
no query changes.

**Alternatives considered:**
- Rewrite the queries against the new tables. That is about 15 query sites in `queries.py` and
  `timetable.py`, two code paths to keep equal, and no gain over the view.
- Store whole variants per train (default plus differences, the "template" form for one train).
  It only removes duplication within a train, which gives −45% on the TT feed (4.35 M to 2.41 M
  distinct train/stop/time rows). Shared patterns also remove it across trains, giving −68%.

### D2. Pattern identity and start time

A variant's `start_sec` is its earliest scheduled time, `min(coalesce(dep, arr))` over its
stops. That is NULL only if no stop has a time, and then every offset is NULL too. A pattern is
the ordered list of `(stop_sequence, station_id, source_stop_id, platform_code, arr − start,
dep − start, pickup_type, drop_off_type)`. Every stored field except the train, the variant's
`path_sha1`, the feed and the id is part of the pattern, so two variants share one only when
nothing distinguishes them but their start time. The `path_sha1` stays on the variant, because
it names the route and the same pattern can belong to differently named variants.

The 2026-10-05 measurement found 213,575 patterns and 1.78 M pattern stops for 742,134 variants
(5.54 M rows).

### D3. Original row ids, stored as runs

Ids are kept exactly, so the `order_by(StationStop.id)` fallback, the board's
`(station_stop.id, service_date)` keys and the API's `station_stop_id` all stay as they are. Per
variant, rows in stop-sequence order are split into runs of consecutive ids. The view computes
the id with a correlated lookup of the run starting at or before the position:

```
first_id + position − from_position
```

That gives 965,426 run rows: three integers each, an estimated 25–35 MB.

**Alternative considered:** new ids of `variant_id * 1000 + position`. It is smaller and the view
is simpler, but `station_stop_id` and the no-run fallback would then differ from the collection
database, and the parity requirement would need an exception. It is kept as the fallback if the
correlated lookup makes boards too slow (task 3.2 measures it).

### D4. Route-variant run summary

`train_variant_run(train_id, path_sha1, last_service_date, n_runs)` holds one row per
`(train_id, path_sha1)` over all of `trip`, the NULL variant included. Because a train has at
most one run per date:

- The most recent variant, the variant of the latest run over non-NULL variants, is the non-NULL
  row with the largest `last_service_date`. That row is unique.
- The ranking `(max(service_date), count(*))` per variant is `(last_service_date, n_runs)`.

Both server reads go through one helper in `queries.py`. It reads the summary when the database
has `train_variant_run` (checked once per engine) and aggregates `trip` otherwise, so the
collection database keeps working unchanged. A test runs both branches on the same fixture and
compares them.

The summary is computed with one `GROUP BY` over the collection database's 13.6 M `trip` rows.
The 2026-10-05 day-type measurement ran the same kind of query in 36 s with 1.6 GiB of peak
memory. It has at most 742 k rows.

### D5. Build order and verification

1. Copy the unchanged tables, as today.
2. Stream the source `station_stop` ordered by `(feed_version_id, train_id, path_sha1,
   stop_sequence)`. Per variant, build its pattern text, look up or create the pattern by
   digest, write `train_variant` and `variant_id_run`. Memory stays at the pattern digest map
   (about 214 k entries).
3. Write `train_variant_run` and the notice-rate totals, which already verify the fingerprint.
4. Verify: stream the source rows and the view in the same order, and compare every field,
   id included. Compare the counts per feed. On the first difference, stop, name the train,
   variant and stop sequence, and fail.
5. `ANALYZE`, `VACUUM`, and print the size and counts (spec: "The build reports its size").
   Without statistics, SQLite answered a board through the view by scanning every variant of the
   feed instead of starting from the station's pattern stops. On 2026-10-05 `ANALYZE` took Köln
   Hbf from 0.84 s to 0.65 s at no size cost. A covering variant index added 44 MB for another
   0.02 s and was left out.

The build writes to `<out>.partial` and renames it over `<out>` only after step 4 passes. A
failed build removes the partial file and leaves `<out>` as it was.

### D6. Parity check against the collection database

`scripts/compare_deployed_db.py` calls the web layer's functions in one process against two
engines: the collection database and the deployed copy. It uses:

- `station_board` with a fixed `now`
- `train_route` with and without `station_id`
- `line_directions`, `line_stations` and `line_detail`
- `trips_around` and `corridor_trips_around`, the Prognose flow's earlier and later runs, for
  each direction at a few stops and times of day
- `suggest`

It covers a fixed sample of stations (including the three busiest), trains with several variants
(including NX 26405), lines (including the Aachen–Dortmund RE4 at Wuppertal Hbf and the
Schwerin–Szczecin RE4, whose runs differed on today's 60-day copy on 2026-10-05) and line-less
trains. It compares the JSON exactly. It runs as a capped
job, and its result is recorded in tasks.md. The fixture tests cover the same equality on small
data.

### D7. Boards order same-time rows by id

`station_board` takes its first 200 candidate rows per slice ordered by time only. At a busy
station that limit falls among rows with the same time, so which of them survive depended on the
query plan, and the same data gave different boards on two copies (46 and 47 calls at Düsseldorf
Hbf, 2026-10-05). The board now orders by `(time, station_stop.id)`. Ids are kept exactly (D3), so
both databases cut at the same row. The collection database's boards become deterministic too.

The runs before and after a time (`_trips_around_in`) had the same flaw. They sorted candidate
calls by time alone, so two runs at the same minute (RE 13205 and RE 90105 at Malchin, 07:43,
found by the 2026-10-05 parity check) came out in query order. They now sort by `(time,
station_stop.id)` as well.

### D8. Further lossless compaction (added 2026-10-05, before task 3.4)

The first real build was 518 MiB. Of that, about 98 MB was repeated 40-character route hashes,
43 MB repeated route-path text, and about 70 MB key and index structures the server never reads.
The user asked for these to go before the old copy is replaced. Each item keeps every value the
server reads, which the build checks row by row and the parity check checks response by response:

- **A. Route hashes become numbers.** `variant_path(id, path_sha1)` holds each distinct hash once
  (about 112 k, from trains, schedule variants and runs). `train_variant`, the run summary and the
  trains store the number. Views give back the text.
- **B. Route paths are stored once,** in `route_text(id, route_path)`. On real data each hash stands
  for one path, but the test fixture has two trains with one hash and two paths, so the paths get
  their own lookup and nothing depends on that pairing.
- Every lookup uses id 0 for "none", so the views are plain inner joins that SQLite can merge into
  the query. The lookups' text indexes are needed only while building, and the build drops them
  at the end.
- **C. Tables keyed by two columns are `WITHOUT ROWID`:** `pattern_stop`, `variant_id_run`, the
  run summary and `notice_rate_total`. Their data is then not stored a second time as a key index.
- **D. Build-only and unused structures go.**
  - `stop_pattern` is dropped; pattern digests stay in the builder's memory.
  - `train_variant`'s two lookup indexes merge into one on `(train_id, feed_version_id, path_id)`.
  - `train` keeps only the indexes the server filters by: `id`, `(line_id, train_number)` and
    `(category, train_number)`. The collector's identity, route, number and search-name indexes
    are dropped.
  - `search_alias` drops its integrity-only unique index. Search uses `ix_alias_lookup`.
- **E. The run summary is one table.** It is `variant_run_data(train_id, path_id,
  last_service_date, n_runs)`, keyed on its first two columns, with path 0 for runs without a
  hash. `train_variant_run` becomes a view with the same columns, so the server's helper reads it
  unchanged.
- **F. Origin and destination names are stored once** in `place_name`. `train` becomes a view over
  `train_data` and the three lookups that gives back every column of the collection database's
  `train`.

The build also checks every `train` row and every run-summary row against the collection database,
not only the schedule.

## Risks / Trade-offs

- **[Measured] Boards are slow for another reason.** On 2026-10-05 real board times were 1.2–16.5 s
  on both the old and the new copy. 98% of that is the known direction bug in
  `direction._connected_components`, which is outside this change. With directions cached, the view
  costs about 0.1 s more than a plain table after `ANALYZE` (Köln 0.65 s against 0.53 s).
- **[Risk] Boards get slower through the view.** The time filter applies to a computed column,
  so the station's index narrows only to the station's rows (22 k at Düsseldorf), and the id
  lookup runs per candidate row. → Task 3.2 measures the busiest stations. If the time goes over
  0.5 s, first add `(pattern_id, position)`-covering indexes. If it is still slow, fall back to
  new ids (D3 alternative) and record the API consequence.
- **[Risk] SQLAlchemy treats the view differently from a table.** Reads are identical in SQLite.
  The server never inserts. → The fixture tests run the existing train-route and board tests on a
  pattern-built copy.
- **[Risk] The estimate is off.** The 0.5–0.6 GB figure is computed from per-row sizes, not
  built. → Task 3.1 builds the real file and records its size. Nothing else depends on the
  number.
- **[Trade-off] The deployed copy is read-only by construction.** Any future runtime write
  (none exists) would need the real table. That is accepted: the copy is rebuilt for every
  deployment.
- **[Risk] The build takes longer.** It adds a streaming pass over 5.5 M rows twice (build and
  verify). → An estimate of a few minutes in all, which task 3.1 measures. It runs under
  `dbrel ml launch --memory 4G`, like today's build.

## Migration Plan

1. Build the new copy next to the old one (`--out data/deploy/slim-patterns.sqlite`), run the
   parity check, compare the sizes.
2. Replace `data/deploy/slim.sqlite` and upload it to the Fly.io volume, as in the README's
   "Deploying" section. The server code with the summary helper must be deployed first or in the
   same step, because the new copy has no `trip` table.
3. Rollback: upload the previous file. The server works with either, since the helper falls back
   to `trip`.
