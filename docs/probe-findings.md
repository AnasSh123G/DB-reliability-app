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

---

# Part III — the archival reader, 2026-09-12

Measured on the 21-station Rhein-Ruhr corridor (Köln → Hamm, plus the Wuppertal and
northern Ruhr branches), one `/plan` roll of 84 station-hours followed by one `/fchg`
sweep. These supersede two figures in Part II.

## 20. `/fchg` retains passed events for ~15 hours, not 75 minutes

§18 could only establish a **floor**: every event survived the pilot's 75-minute watch,
because the watch ended before the API dropped anything. Measured against the clock
instead of against a watch, over 717 recorded values:

| age of value when read | values |
|---|---|
| under 1 h | 246 |
| 1–2 h | 108 |
| 2–8 h | 302 |
| 8–15 h | 61 |
| oldest seen | **905 minutes** |

The decay is smooth rather than a cliff, and 89 % of values were under 8 hours old when
read. So retention is
an order of magnitude longer than §18 could show.

**Consequence:** the archival strategy does not need frequent polling. A value can be
read hours after the event and still be the recorded one, which is what makes
`QUALITY_RECORDED` sound — and what lets `TT_LINGER_S` sit at 6 hours, aligned with
`SERVICE_DATE_CLOSE_S`, so the poll-coverage window matches the period in which grades
are still allowed to change.

## 21. §19's per-station stop count was off by 7×

§19 states `/fchg` returns "~2,170 stop events per station per poll". Measured: **9,950
stops across 21 stations, a mean of 474 per station** (range 93 at Castrop-Rauxel to 1,119
at Köln Hbf). 2,170 was the per-*sweep* total across the eight pilot stations, mislabelled
as per-station. The bandwidth conclusions in §19 are unaffected, since bytes were measured
directly.

## 22. Four of 21 EVA mappings needed a human, all of them §13 cases

Auto-resolution got 17/21. The four failures are precisely the systematic naming
disagreements §13 catalogued, and each needed `--manual`:

| GTFS (DELFI) name | IRIS name | eva | why it failed |
|---|---|---|---|
| `Hamm (Westf), Hauptbahnhof` | `Hamm(Westf)Hbf` | 8000149 | DELFI comma suffix |
| `Herne Wanne-Eickel Hbf` | `Herne-Wanne-Eickel Hbf` | 8000192 | IRIS's extra hyphen (§14) |
| `Köln Messe/Deutz Bf` | `Köln Messe/Deutz` | 8003368 | `/` in the name → HTTP 404 |
| `Neuss Hauptbahnhof` | `Neuss Hbf` | 8000274 | Hbf vs Hauptbahnhof (§13) |

Also note `Hamm (Westf)` **does** exist as a parent station in the free fv+rv feeds,
contradicting §13's claim that it and Münster have none.

**A new instance of the §14 trap, worth recording:** querying `/station/KNE` — a plausible
guess at Neuss's DS100 — returns **`Neuwied`**, eva 8000276. A rail eva, in range, wholly
the wrong city. Neuss is DS100 `KN`, eva 8000274. DS100 codes must be verified against the
returned name, never trusted because the eva looks valid.

## 23. `/plan` coverage is the binding constraint, and the alarm now works

The first sweep matched 1,486 of 9,950 stops and sent **8,464 to `tt_unmatched`** — because
`/plan` had only been rolled for 4 hours while `/fchg`'s horizon spans ~15. The residue is
not noise: it is the measure of how far `/plan` coverage lags, and it was previously
invisible, since `n_unmatched` could never fire and `tt_unmatched` was never written.

Separately, **118 runs were created directly from `/fchg`** in that one sweep — entries
carrying a full `<tl>` + `pt` record, i.e. the additions and replacements `/plan`
structurally cannot contain. Under the old `keep=` predicate all 118 were discarded before
an object existed.

## 24. `/plan` drops past hours of *today* after roughly 14 hours

§15 established that `/plan` serves today and tomorrow and 404s for yesterday. It did not
ask how much of **today** survives, and the answer is: not all of it.

Measured on 2026-09-14, importing `--date 2026-09-14 --hours 0-23` across 15 stations at
**22:39 local**, 360 station-hour buckets:

| hours | result |
|---|---|
| 00–08 | **404 at every one of the 15 stations** (135 buckets) |
| 09–23 | 200 at every one of the 15 stations (225 buckets) |

The cut is clean, uniform across stations, and sits about 13–14 hours behind the wall
clock. So `/plan` keeps a rolling window of roughly the last ~14 hours of today plus the
remainder of today and tomorrow; anything older is purged even though the calendar date is
still "today".

Consequences:

- **There is a partial backfill, and it is worth taking.** A one-off
  `tt-import-plan --date <today> --hours 0-23` recovers ~15 hours of schedule that the
  3-hour rolling importer would never have fetched — for a database started mid-day this
  is the difference between a few hours of usable history and most of a day. It cost 360
  calls and ~7 minutes at 50 rpm.
- **But it is not a full day**, and the gap is silent: the missing buckets return 404,
  which `import_plan` counts as `buckets_missing` with no error, exactly like a genuinely
  empty hour. Do not infer from a clean run that the whole day was captured.
- §15's "captured inside its window or gone for good" therefore stands, with the window
  being ~14 hours wide rather than one hour. The hourly cron remains mandatory.
- Downstream, a stop whose `/plan` bucket was never fetched has no planned time, and the
  poller's "no planned time ⇒ no delay to state" rule discards the `/fchg` value for it.
  Missing early-morning plan buckets thus cost actuals too, not just schedule.

## 25. Station name resolution collapses badly once minor stops are in scope

§13/§22 established the naming-disagreement problem at 21 stations, all major interchanges
(Hbf-type), where 17/21 resolved automatically. Expanding the tracked set to 238 stations
within 40 km of Bochum Hbf — ranked by traffic, so most of them are minor suburban and
S-Bahn stops rather than hubs — dropped the automatic hit rate to **53/238 (22%)**.

The dominant, previously-undocumented cause: **IRIS hyphenates `City District` compounds
that gtfs.de/DELFI spaces.** `Bochum Dahlhausen` (GTFS) is `Bochum-Dahlhausen` (IRIS);
confirmed live against a sample of ten Ruhrgebiet suburb stops, 9 hit on the hyphenated
form:

| GTFS name | IRIS name | eva |
|---|---|---|
| `Bochum Ehrenfeld` | `Bochum-Ehrenfeld` | 8001039 |
| `Bochum Hamme` | `Bochum-Hamme` | 8006710 |
| `Bochum Langendreer` | `Bochum-Langendreer` | 8000358 |
| `Bochum Riemke` | `Bochum-Riemke` | 8001035 |
| `Duisburg Hochfeld Süd` | `Duisburg-Hochfeld Süd` | 8001605 |
| `Essen Borbeck` | `Essen-Borbeck` | 8001902 |
| `Essen Kray Süd` | `Essen-Kray Süd` | 8001911 |
| `Dortmund Kirchhörde` | `Dortmund-Kirchhörde` | 8001534 |
| `Wattenscheid Höntrop` | `Wattenscheid-Höntrop` | 8006227 |

Only the *first* space is hyphenated — a multi-word district name keeps its own spacing
(`Duisburg-Hochfeld Süd`, not `Duisburg-Hochfeld-Süd`). `Bochum Höntrop` was the one miss
in the sample (`Bochum-Höntrop` returns no match at all — the eva-less GTFS entry may not
exist in IRIS under any spelling).

**A second, equally systematic pattern, found the same way: gtfs.de appends a bare
trailing `S` to an S-Bahn-only stop that IRIS's name never carries at all** —
`Essen Steele S` (GTFS) is `Essen-Steele` (IRIS), combining with the hyphenation above.
This one alone accounted for roughly 20 of the remaining misses after the hyphenation fix,
concentrated in the Essen, Erkrath, Ratingen and Düsseldorf S-Bahn network:

| GTFS name | IRIS name | eva |
|---|---|---|
| `Essen Steele S` | `Essen-Steele` | 8000099 |
| `Essen Steele Ost S` | `Essen-Steele Ost` | 8001913 |
| `Essen Eiberg S` | `Essen-Eiberg` | 8001905 |
| `Essen Frohnhausen S` | `Essen-Frohnhausen` | 8001906 |
| `Essen Hügel S` | `Essen-Hügel` | 8001908 |
| `Essen Werden S` | `Essen-Werden` | 8001915 |

Not universal — `Essen Kettwig S`, `Erkrath Hochdahl S` and `Ratingen Hösel S` still return
no match under either spelling, so some of these stops may simply not exist in IRIS at all,
or use a name unrelated to the GTFS one.

Fixed in `query_variants` (`timetables/stations.py`): both a hyphenated fallback and an
S-suffix-stripped-then-hyphenated fallback are now generated whenever the cleaned name
contains a space, tried only after every earlier variant — including the untouched
original name — has already failed. Together the two fixes raised the automatic
resolution rate at the 238-station scale from 53/238 (22%) to 132/238 (55%).

Also observed, not yet worth a general fix: a `City, Bahnhof`-style GTFS name (`Altena,
Bahnhof`, `Bork, Bahnhof`) frequently fails outright even after the existing comma-splitting
variant, because `/station/` returns an unrelated substring match rather than 404
(`Bork` → `Borkum Reede`, a different place with a non-rail eva) — a second instance of the
§14 trap, not a new pattern. These remain `--manual` cases.

Consequence for scale: expanding past a handful of major hubs means budgeting for a
meaningfully lower automatic resolution rate, and treating the unresolved list as
expected — not a sign something is broken — for genuinely obscure halts.

**TODO, not yet done:** at the 238-candidate/40 km scale, both fixes above raised the hit
rate from 53/238 (22%) to 144/238 (60%), leaving 94 unresolved. The next systematic pattern
worth fixing is the Rhein-Ruhr city-abbreviation prefixes DELFI uses that IRIS spells out in
full — `D-Angermund S`, `OB Sterkrade Bf.`, `BOT Feldhausen Bf`, `DO-Knappschaftskrankenhaus`,
`DU-Obermeiderich Bf`, `GE Buer Nord Bf`, `ME-Neanderthal S` all fail, while the spelled-out
form resolves directly (confirmed: `Düsseldorf Flughafen` → eva 8000082, where `D-Flughafen`
finds nothing). Needs a lookup table of the abbreviations in use (D, DO, DU, BOT, OB, GE,
ME, …) tried as another `query_variants` fallback, same shape as the two fixes above. What's
left after that is genuine `--manual` territory: names IRIS doesn't have under any spelling,
and wrong-match rejections like `Bork` → `Borkum Reede` that need a human to find the right
eva. See the README's "Notes and limits" for the operational summary of this TODO.

## 26. A one-shot full-day `/plan` backfill cannot survive its own runtime past a few hundred stations

The 212-station backfill on 2026-09-16 (`tt-import-plan --date 2026-09-16 --hours 0-23`,
22:05–23:45 CEST) produced 2,000/5,088 buckets as 404 — at first glance "no service", but
it isn't:

- **Bochum Hbf and Dortmund Hbf — the busiest stations in the set — 404'd for hours 6, 7
  and 8**, i.e. the morning commuter peak. No real-world timetable has zero trains at a
  major Hbf during rush hour, so this cannot be a scheduling gap.
- **The 404 boundary lines up with wall-clock time, not the station or the hour.** For
  bucket `(2026-09-16, hour=9)` specifically, the 120 stations reached earliest in the run
  got HTTP 200; the 92 reached later got 404. Split point: last success `23:01:26`, first
  404 `23:01:54` — almost exactly 14 hours after `09:00`, matching §24's "`/plan` drops past
  hours of *today* after roughly 14 hours" exactly. The backfill started at 22:05
  (22:05 − 14h ≈ 08:05), so every station reached after its target hour turned 14h stale
  got a 404 regardless of what actually ran.

**Consequence, generalised to all of Germany (8,215 rail parent stations, §6):** a full-day
backfill needs 8,215 × 24 = 197,160 `/plan` calls, i.e. ≈65.7 hours at 50 rpm — more than
4× the 14-hour window that makes any given hour's data recoverable at all. This is not
solved by reordering the sweep. Sweeping hour-outer instead of station-outer (all 8,215
stations for hour 0, then all for hour 1, …) still needs `8,215/50rpm ≈ 2.74h` per
hour-wide pass, and hour `h`'s own deadline is `h+14` (hours after day-start), so the sweep
only keeps up while `(h+1) × 2.74 ≤ h+14` — true only through **h ≈ 6**. Past that, every
extra hour of schedule costs 2.74h of sweep time but only buys 1h of extra deadline slack,
so the gap widens monotonically. **No ordering of a flat national sweep can capture more
than roughly the first 6-7 hours of a day**; the rest is structurally unreachable within a
single pass, not merely slow to reach.

The same ceiling reappears in the *recurring* cycle, not just the one-time backfill: keeping
even a short (~8h) rolling schedule window fresh across 8,215 stations costs `8,215 × 8 /
50rpm ≈ 21.9h` per cycle — again far longer than the `TT_LINGER_S` (6h) ceiling the whole
cadence design is built around.

**TODO, not yet done:** national coverage cannot be a bigger version of the current
station-outer/hour-inner backfill — it needs a fundamentally different shape: never attempt
to recover history, only ever request a short, near-term lookahead window (a few hours) for
every station, continuously, so requests are always made well before their retention
deadline rather than racing it. Even that is expensive at 8,215 stations (a 4h-lead sweep
alone is ≈11h/lap), so priority tiering (§ the `poll_priority` mechanism already in
`changes_poller.py`, not yet extended to `plan_import.py`) is not an optimisation at
national scale, it is a hard requirement: most of Germany's ~8,215 stations would need to be
checked far less often than a curated set of hubs and corridors.

## 27. The advertised 60/min limit is not enforced — at least not within a 30-second burst

Every prior finding in this log, and the whole `TT_RPM` design (config.py), assumed the
`x-ratelimit-limit: name=default,60` header was a real, enforced ceiling. Tested directly
against the live gateway with two separate client-id/api-key pairs, using raw `httpx` calls
that bypass dbrel's own client-side `TokenBucket` entirely, it is not — or at least not at
the volumes and durations tested:

| test | pace | result |
|---|---|---|
| pair 1 alone, `/station` | 400 req in 9s (~2,660/min) | 400×200, 0×429 |
| pair 1 alone, `/station`, extended | 1,500 req in 34s (~2,660/min) | 1,500×200, 0×429 |
| pair 1 alone, `/fchg` | 300 req in 7s (~2,560/min) | 300×200, 0×429 |
| pair 2 alone, `/station` | 550 req total, up to 45 req/s | 550×200, 0×429 |
| both pairs concurrently | 90 req in 1s (45 each) | 90×200, 0×429 |

`x-ratelimit-remaining` behaves inconsistently with either key actually being blocked: pair
1's counter (the older, previously-used credential) counts down from a real number and
clamps at `0`; pair 2's (freshly subscribed, never used before this test) reported `0` from
its very first request ever made. In both cases, reaching `0` never produced a single 429 —
the header looks like bookkeeping that is not wired to an enforcement path, at least for the
Timetables product's current tier.

**This does not resolve "per-key vs shared account limit"** — the question the two-pair test
was originally built to answer — because neither key is throttled at all under these
conditions, so the question does not have a meaningful answer right now.

**Caveats, both real:** (1) this is a ≤34-second burst test, not an hours-long sustained one,
so a longer-window (hourly/daily) cap elsewhere cannot be ruled out; (2) whatever is causing
this could be a temporary gateway state rather than a permanent policy change — DB could
switch enforcement back on without notice.

**Consequence, implemented rather than just noted:** the client-side limiter
(`timetables/ratelimit.py`) no longer runs at a fixed, pessimistic rate. It runs fast by
default (`TT_RPS_FAST`, 60/s — an order of magnitude above the advertised minute-based
figure) and degrades only on evidence: a real 429 triggers `penalise()`, which pins every
process sharing the lock file to `TT_RPM_PENALTY` (58/min, the advertised-safe rate) for
`TT_PENALTY_S` (180s), then returns to fast mode on its own. This is deliberately not "trust
the measurement and remove the limiter" — caveat (2) above is exactly why a real 429 must
still produce real, immediate, cross-process backoff rather than being ignored.

Measured effect on the full-day, 212-station backfill (5,088 `/plan` calls): **≈70 seconds**,
down from ≈88 minutes at the old fixed 58/min. A full cold-start-to-first-cycle run completed
in 120s against the live API, with zero 429s observed.

## 28. §17's wings understate the problem — stopIndex itself shifts with coupling

§17 established that wings are real and identified by run key. What it didn't establish: a
train that sometimes runs solo and sometimes coupled to its wing partner gets a **different
stopIndex for the same physical station** depending on which it did that day, and nothing in
the schema recorded which variant a given day's numbering belonged to.

Concrete case, found via the national 30-day backfill (2026-08-24..2026-09-22): RB 30007 and
30057 (line RB20) both start at Alsdorf-Annapark and run coupled through Eilendorf before
splitting further down the line — 30007 continues to Eschweiler-Weisweiler, 30057 to
Stolberg-Altstadt. On days 30007 ran solo, Eilendorf landed at `stopIndex` 11 in its own
numbering. Once coupled, the combined consist's numbering pushed Eilendorf to `stopIndex` 17,
and index 11 in that numbering belonged to a different real station (Herzogenrath, per the raw
`/plan` XML for 2026-09-01: eva 8002806, `pt=`06:43).

`station_stop` (the planned pattern) was keyed on `(train_id, stop_sequence)` alone, on the
assumption — reasonable for an unwinged train, wrong for one that sometimes isn't — that a
train's stopIndex-to-station mapping is permanent. Once one day's numbering populated index
11 as Eilendorf, a later coupled day's real index-11 observation (Herzogenrath) was graded
against Eilendorf's *planned* time instead of its own, because `changes_poller.py` trusted the
pattern lookup and never cross-checked the observed eva. The result: `trip_stop` rows with an
apparent delay around **-1560s** ("26 minutes early") that were in fact correctly-timed
observations of the wrong station, plus the same physical station appearing twice under two
stop_sequences for the same trip.

**Scale**: 7,453 of 73,269 trains (~10%) exhibited the collision; 472,612 `(trip, station)`
pairs held more than one stop_sequence; 416,531 `RECORDED` stops carried `arr_delay_s < -600`
across the full backfill window — all symptoms of the same root cause, not independent defects.

**Fix**: `runs.py`'s per-run route digest (`path_sha1`, computed since §10/§11 to distinguish
genuinely different routings under one train number) is now also the key `station_stop` and
`trip` are stored and looked up under, alongside `(train_id, stop_sequence)`. A station's
planned slot is therefore scoped to *which routing ran that day*, not just which train number —
so a coupled day's observations are graded against the coupled numbering's own pattern, and a
solo day's against its own, instead of one clobbering the other. `changes_poller.py` additionally
cross-checks the observed eva against the planned station before writing a fact, routing a
genuine mismatch to `tt_unmatched` rather than ever writing a corrupted `RECORDED` row again —
a second, independent guard against the same class of bug recurring under some other cause.

**Consequence for anything built on `trip_stop` from before this fix**: a full backfill window
graded under the old single-pattern-per-train assumption is not just missing some data, it
contains confidently-wrong `RECORDED` values, silently indistinguishable from genuine ones
without re-deriving from the raw archive. Any stored aggregate (see `stop_delay_average`) built
on such a window needs the window re-replayed under the fix, not patched in place.
