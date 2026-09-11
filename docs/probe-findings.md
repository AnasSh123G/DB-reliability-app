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

---

# Part II — DB Timetables API (IRIS-TTS), 2026-09-10

Probed with `scripts/probe_timetables.py` against the eight-station Bochum pilot set
(Bochum, Dortmund, Essen, Gelsenkirchen, Herne-Wanne-Eickel, Hagen, Herne,
Castrop-Rauxel). Sample: 2,113 planned stop events over hours 05–22 of 2026-09-11.

## 9. Both auth headers are required, and the rate limit is advertised

`DB-Client-Id` **and** `DB-Api-Key` must both be sent. Sending either alone returns 401
with the same body as sending neither, so a credential smoke test has to try the
combinations separately to say which half is wrong.

The gateway advertises its budget in response headers:

```
x-ratelimit-limit:     name=default,60;
x-ratelimit-remaining: name=default,59;
```

60 requests/minute, named `default`. No daily-cap header is advertised.

## 10. `(category, number)` is a sound cross-day train identity

1,108 distinct `(tl@c, tl@n)` pairs in the sample; **0** had more than one daily trip id.
So the identity key can be `(category, number)` without the operator, which is what makes
a stable cross-day series possible. `tl@n` was present on **100 %** of 2,113 stops and
numeric on all of them, so `train_number` is an `Integer`.

Categories seen: `S 659, RB 336, NX 270, RE 254, VIA 203, ERB 146, ICE 140, IC 40,
RRB 39, TRI 10, FLX 8, NJ 4, Bus 3, EUR 1`.

## 11. The line/number split is exactly along the FV/RV boundary

`dp@l` (line) is present for **every** regional and S-Bahn stop and absent for **every**
long-distance one:

| with `l` | without `l` |
|---|---|
| S, RB, NX, RE, VIA, ERB, RRB, TRI, FLX | ICE, IC, NJ |

`l` never varies between the stops of one run (0 of 1,001 runs). This confirms the
two-entity model: long distance is searched by **number** ("ICE 599"), regional by
**line** ("RE 4", "S1"), and one table cannot serve both.

## 12. The stop id is 1-based, and the swagger prose is wrong about it

`id = <dailyTripId>-<YYMMddHHmm>-<stopIndex>`.

- The middle field is **10 digits** on 2,113/2,113 stops — date *and* time of departure
  from the origin. The swagger prose calls it "a 6-digit date specifier"; its own worked
  example is 10 digits. The prose is wrong.
- `stopIndex − len(ar@ppth)` was **1 on all 1,588** stops that have an arrival, and the
  525 origin stops (no `ar`) all carry index **1**. So `stopIndex` is the trip-wide
  ordinal, **1-based**, usable directly as `stop_sequence`. The continuous invariant is
  `len(ar@ppth) == stopIndex - 1`.
- Daily trip ids are frequently negative (`-2619682142194285232-2609110521-2`), so the id
  must be parsed with `rsplit("-", 2)`, never `split("-")`.

## 13. `ppth` station names do NOT reliably resolve to gtfs.de names

This is the most consequential finding, and it is worse than expected. Of 432 distinct
`ppth` names in the sample, matched against all 7,824 gtfs.de parent stations:

| matching rule | distinct | weighted by occurrence |
|---|---|---|
| casefold + strip accents | 126 (29.2 %) | 25.7 % |
| additionally punctuation-free | 276 (63.9 %) | 71.8 % |

The residue is not noise, it is four systematic naming disagreements:

| IRIS | gtfs.de (DELFI) |
|---|---|
| `Neuss Hbf` | `Neuss Hauptbahnhof` |
| `Herford`, `Schwerte(Ruhr)` | `Herford, Bahnhof/ZOB`, `Schwerte, Bahnhof` |
| `Gütersloh Hbf` | `Gütersloh, Hbf/ZOB` |
| `Wattenscheid` | `Bochum Wattenscheid Bf` |

`Hamm(Westf)Hbf` and `Münster(Westf)Hbf` have no corresponding parent station in the free
fv+rv feeds at all.

**Consequence for the schema:** name matching cannot be the join between the two sources.
`ppth` is stored verbatim as the route skeleton, and `station_id` resolution from a name is
a nullable convenience, never load-bearing. The GTFS↔Timetables join must go through
**EVA ↔ station coordinates**, not names.

## 14. Station name lookup returns non-railway stops, and can miss entirely

`/station/{pattern}` is a weak resolver. For Herne-Wanne-Eickel:

| query | result |
|---|---|
| `Wanne-Eickel Hbf` | no match |
| `Herne Wanne-Eickel Hbf` (the gtfs.de name) | no match |
| `Wanne-Eickel` | `Wanne-Eickel Hauptbahnhof, Herne` — **eva 212342, a bus stop** |
| `EWAN` (DS100) | `Herne-Wanne-Eickel Hbf`, eva 8000192 — correct |

IRIS spells it with an extra hyphen, so the name search misses, and the near-miss query
returns a plausible-looking wrong answer. Two defences, both now in the probe:

- Reject any eva outside **8000000–8099999**; six-digit evas are DELFI bus/tram halts.
- Prefer the DS100 code where it is known.

`/station/` returns no coordinates, so it cannot self-verify — the neighbour-overlap check
against `ppth` remains necessary.

## 15. `/plan` serves only today and tomorrow

`/plan` returned 200 for offsets 0 and +1 days and **404 for −1, +2, +3, +5, +7, +10, +14
and +21**. Within the sweep, 64 of 144 station-hour buckets for tomorrow 404'd, so even
tomorrow is not fully published in advance.

Consequences:

- A `/plan` bucket must be captured inside its window or the planned data is **gone for
  good** — there is no backfill. The hourly rolling importer is mandatory, not a
  convenience.
- Nothing like GTFS's 30-day horizon exists, so the GTFS skeleton remains the only source
  of "what should run" beyond tomorrow, and the only basis for a `MISSING` grade.

## 16. `/plan` carries no status, no messages, and an undocumented label

Over 2,113 planned stops: `ar@ps` and `dp@ps` were **absent on every one**, and **zero**
stops carried an `<m>` message. So planned status and delay causes come only from
`/fchg`. Planned platform `pp` was present on 2,101/2,113.

`tl@t` was `p` on every stop; `tl@f` distributes as `S 659, N 590, D 349, none 335,
F 180`. Undocumented in the swagger but present on every event: **`fb`**, a display label
such as `fb="ICE 843"` — the category-plus-number string, supplied by the API.

## 17. Wings are real and their ids are run keys

0 stops carried `<ref>`, 0 carried `@tra`, but **5 carried `@wings`** — so in `/plan` a
number change does not appear, while wings do. The wings value is a **run key**
(`<dailyTripId>-<YYMMddHHmm>`), not a full stop id, so it joins directly to a `trip`:

```xml
<s id="-5962154382149198734-2609110526-6">
  <tl f="F" t="p" o="80" c="ICE" n="843"/>
  <ar pt="2609110646" pp="16" fb="ICE 843" wings="4287727884008610000-2609110526"
      ppth="Köln Hbf|Düsseldorf Hbf|Düsseldorf Flughafen|Duisburg Hbf|Gelsenkirchen Hbf"/>
  ...
</s>
```

## 18. `/fchg` publishes punctual trains too — so PLAN_HELD is NOT needed

Measured over 153 sweeps of the eight pilot stations at 30 s, 2026-09-10 20:00–21:15,
against the 929 planned stop events in the same window:

| | |
|---|---|
| planned events that ever appeared in `/fchg` | **929 (100.0 %)** |
| planned events that ever carried a `ct` | 903 (97.2 %) |
| planned events never seen | **0** |
| observations where `ct == pt` (no-op) | 909 |
| distinct events in `/fchg` (wider horizon) | 3,733 |
| events whose value ever changed | 450 |

**This refutes the assumption the design was built on.** The plan reasoned that `/fchg`
returns only *changes*, so a punctual train would never appear, and that grading its
silence `NO_DATA` would discard the on-time population and bias every statistic late —
hence a new `PLAN_HELD` grade. In fact IRIS publishes an entry for essentially every stop
in its horizon, punctual ones included, with `ct` set equal to `pt`.

Consequences:

- **`PLAN_HELD` is removed.** Absence from `/fchg` does not mean "ran to plan"; it means
  out of horizon or genuinely unobserved, which is what `NO_DATA` already says. The
  situation is much closer to GTFS-RT than expected, and rule v1's treatment of absence
  carries over unchanged.
- On-time running is recorded as a real observation with `ct − pt = 0`, so it reaches
  `OBSERVED` on the normal path.
- The per-station listening record (finalize 5.1) is unaffected and still essential.

**Linger:** every event stayed in `/fchg` for the full 75-minute window — median, p90 and
max are all 75.0 min, i.e. the measurement was bounded by the watch, not by the API.
Events are not withdrawn shortly after passage, so a value can be seen settling and
`SETTLE_S = 300` is comfortably achievable at a 30 s cadence.

**Cost:** 1,248 requests over 75 minutes, all HTTP 200, no 429s at ~16 rpm alongside other
traffic.

## 19. `/fchg` bandwidth is NOT negligible — a plan estimate corrected

Measured over 808 successful polls of 8 stations at a 30 s cadence:

| | |
|---|---|
| mean response | **154 KB** |
| total over 50 min | 121 MB |
| projected at this cadence | **~3.5 GB/day** |
| mean fetch | 31 ms |

The plan claimed "a few MB/day" for a tracked set of this size. That was wrong by three
orders of magnitude, because `/fchg` returns the *entire* forward horizon on every call —
~2,170 stop events per station per poll — not a delta. Compare the nationwide GTFS-RT
feed at ~13 GB/day (47 MB every 5 minutes).

So bandwidth scales with **stations × cadence**, and the honest comparison is:

| setup | requests/day | bytes/day |
|---|---|---|
| GTFS-RT nationwide, 5 min | 288 | ~13 GB |
| Timetables, 8 stations, 30 s | 23,040 | ~3.5 GB |
| Timetables, 8 stations, 5 min | 2,304 | ~350 MB |
| Timetables, 150 stations, 5 min | 43,200 | ~6.6 GB |

Timetables is still far cheaper *per station watched*, and it is the only source of train
numbers, but "a few MB/day" was not true. Two consequences:

- Cadence is now a bandwidth decision as well as a resolution one. 30 s is affordable for
  a pilot; a 150-station set should sit at 3–5 minutes.
- **`/rchg` deserves revisiting.** It was dismissed on the grounds that the binding
  constraint is requests rather than bytes, which is true of the rate limit but not of
  bandwidth. It returns only the last two minutes, so at a 60–120 s cadence it would cut
  transfer by roughly the ratio of horizon to window while costing the same request count.

**Storage, by contrast, is efficient as designed:** 45,011 matched stop events produced
only 2,972 stored observations — **6.6 %** — because unchanged values are discarded.
Without that filter the table would have grown by the full horizon every 30 seconds.
