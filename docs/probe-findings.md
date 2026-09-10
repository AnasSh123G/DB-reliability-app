# Probe findings — gtfs.de static vs realtime

Measured 2026-09-10 with `scripts/probe_match.py` and `scripts/probe_validate.py`,
against static feeds generated 2026-09-05 (5 days stale) and a live realtime fetch.

These are the facts the schema is built on. Re-run the probes if the feeds change shape.

## 1. `trip_id` joins are sound

204/204 shared fv trip_ids (100%) describe the *same journey* in both feeds — same
`stop_id`s, same order, same count. Both id spaces are plain integers, so this was
worth checking: the overlap is identity, not coincidence.

**All gtfs.de feeds share one id space.** `stop_id` 14794 (Aachen Süd) is the same
in `fv` and `rv`, and the rail trips in the 284 MB full feed are exactly the union
of `fv` + `rv`:

| feed | active rail trips (2026-09-10) | present in realtime |
|---|---|---|
| fv | 1,085 | 204 |
| rv | 42,332 | 5,981 |
| **fv + rv** | **43,417** | **6,185** |
| full (284 MB) | 43,417 | 6,185 |

**The 284 MB full feed is therefore unnecessary** — `fv` + `rv` (12 MB) gives identical
rail coverage.

## 2. The realtime feed is a rolling window, not a day

This invalidated the first match-rate measurement. Matched trips have a first
departure between **−11.0 h and +0.7 h** of the feed timestamp: the feed carries
trips already running or starting shortly, never the whole service day.

Scored against *all* trips active today, coverage looks like 14–19%. Scored against
trips **in progress at the feed timestamp** — the only population the feed can cover:

| feed | in progress | with ≥1 stop_time_update | rate |
|---|---|---|---|
| fv | 259 | 172 | **66.4%** |
| rv | 1,782 | 1,581 | **88.7%** |
| all rail | 2,139 | 1,830 | **85.6%** |

fv scores lower because long-distance trips run for many hours, so "in progress"
includes overnight services that carry no realtime data.

**Consequence:** a single poll only ever sees ~1 hour of the schedule. Building a full
day of actuals requires polling throughout the day, and a trip absent from one poll is
not evidence of anything.

## 3. Realtime trip descriptors carry `trip_id` + `start_date` only

All 76,898 trip updates: `trip_id` set, `start_date` set, **`route_id` empty,
`start_time` empty**, `schedule_relationship = 0 (SCHEDULED)` for every single one.

**Consequences:**
- Resolution is a direct `(trip_id, start_date)` lookup. The planned fallback matching
  on `(route, direction, start_time)` is **impossible** — those fields are never sent.
- Nothing unresolvable can be recovered; it goes to `rt_unmatched`.
- **Cancellations are not signalled at trip level** in this feed. No `CANCELED`
  descriptor appeared at all, so cancellation must be detected per-stop via
  `stop_time_update.schedule_relationship = SKIPPED`, or inferred from absence.

## 4. Stop-level update shape

- **929,907 stop_time_updates, 100% keyed by _both_ `stop_sequence` and `stop_id`.**
  So `(trip, stop_sequence)` is a safe primary key, and a train visiting a station
  twice is not ambiguous.
- `time` and `delay` are always populated **together** — 814,352 arrivals and 830,942
  departures each have both. Never one without the other.
- Updates per trip: min 0, p50 8, p90 30, max 79.
- **35% of trips (26,807/76,898) retain stops whose time is already >5 min past.** The
  feed does not aggressively prune passed stops, so the `OBSERVED` quality grade is
  reachable rather than theoretical.

## 5. Times past 24:00:00 are real

Max observed `35:25:00` (127,500 s); **152,109** stop_time values ≥ `24:00:00` in `rv`.
Storing clock times as seconds-from-service-date-anchor is required, not defensive.

## 6. Stops are NOT IFOPT/DHID identified — this changed the schema

**0 of 16,345 `stop_id`s match the IFOPT pattern `de:N:N`.** They are opaque integers.
The planned `station.dhid` natural key does not exist.

Actual structure, and it is clean:

| | fv | rv |
|---|---|---|
| parent stations (`location_type=1`) | 563 | 7,652 |
| child platforms | 656 | 8,693 |
| orphan children | 0 | 0 |
| dangling parent refs | 0 | 0 |
| missing coordinates | 0 | 0 |
| parent names not unique | 0 | 6 |

`stop_times` reference **child** stops; the parent is the station. Columns available:
`stop_name, parent_station, stop_id, stop_lat, stop_lon, location_type, platform_code`.

**Replacement natural key:** normalized `stop_name` + coordinates rounded to 3 decimals
(~110 m). Name alone is insufficient (6 collisions in rv); with coordinates it is unique.
The per-feed-version `stop_id` is still stored for realtime matching.

## 7. Filtering is worth doing early

70,681 of 76,866 realtime trips (**92%**) are bus/tram/other modes outside this project's
scope. Filtering against a prebuilt set of known rail `trip_id`s before constructing any
object keeps a poll cheap — the 47 MB message parses in **0.12 s** with the `upb` backend.

## 8. Feed mechanics

- Realtime: 46.7 MB, `Content-Encoding: none` — **gzip is not offered**, so every poll
  costs the full 47 MB. `incrementality=FULL_DATASET`. Header timestamp was 63 s old.
- Static: strong `ETag` + `Last-Modified`, so conditional GET works.
- `feed_version` in `feed_info.txt` is the constant string `latest-free` — useless for
  versioning. Content hash is the only reliable version identity.
