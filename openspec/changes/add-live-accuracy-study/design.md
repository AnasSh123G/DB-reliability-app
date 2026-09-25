# Design

## Context

The core mechanism (see proposal.md's example): one call to a station returns every train
currently passing through it, each carrying both its scheduled time and — for a train that has
already run — its real recorded time, together in the same response. So one call per station,
for 3,000 stations, is the whole data source: no per-train or per-trip follow-up calls.
Concretely, this is `TimetablesClient.changes(eva)` (the `/fchg` endpoint `dbrel collect`
already polls), read with the existing `dbrel.ingest.parser.parse_timetable`.

TabM's and the average's predictions depend only on a call's schedule — train, station, date,
scheduled time, holiday flag, route position — never on anything that happens between when a
prediction is made and when the trip runs. So it makes no difference whether the prediction is
computed hours before the trip (as it would be for a real user, at 8:00 for a 13:00 train) or,
as this study does, from the same fetch that shows the trip already happened: the number comes
out identical either way, because it's a function of the schedule alone.

One implementation detail: a bare `/fchg` stop doesn't say which stop number a trip is at, or
how many stops its route has — that needs the trip's other stops, which a single station-scoped
call doesn't carry. This comes from `StationStop`, the *static* timetable pattern (populated by
the GTFS/static import, not by continuous collection — it exists for essentially every train
regardless of whether `dbrel collect` has run recently) via `dbrel.ml.timetable.calls_at`, the
same lookup `predict.py`'s live prediction path already uses. This doesn't change the
one-call-per-station shape above — it only fills in two extra numbers the model wants, from
data already on disk (see Decisions).

Also worth being precise about: IRIS's `ct` (changed time) is a live *estimate* for a train that
hasn't arrived yet, not only a record of one that has — its presence alone doesn't mean a side
has happened. `dbrel collect`'s own poller (`changes_poller._has_happened`) already draws this
line correctly: a side counts as realized once its changed (or, failing that, planned) time is
more than a small grace period (120s) in the past. This study reuses that same rule rather than
a naive "does `ct` exist" check.

## Goals / Non-Goals

**Goals:**
- Score TabM and the stored average against real, freshly-observed outcomes the model and the
  averages tables have never seen, using one `/fchg` request per sampled station.
- Reuse the existing feature code, model artifact, and average lookup unchanged — this proves
  something about the deployed system, not a reimplementation of it.

**Non-Goals:**
- Not a new continuous collection mode, and not a replacement for `dbrel collect`.
- Not a guarantee of exactly 3,000 scored comparisons — `/fchg`'s own rolling window decides
  how many of the sampled stations have anything already realized at call time (see Risks).
- Not scoring trips whose route shape can't be found locally (see Decisions, route-shape
  lookup) — those stops are skipped and counted, not scored with guessed values.

## Decisions

**Correction after the first real run (2026-09-25): one `/fchg` call is not enough.** For an
ordinary run `/fchg` is a pure delta: it carries the changed time but no train number and no
planned time (measured: Bochum Hbf, 280 stops, 1 with a train number). The first run therefore
kept only the <1% of stops that are additions/replacements (which do carry both) -- mostly
disrupted trains -- and reported 79% of stations as empty and ~33-minute errors. The fix is the
same split `dbrel collect` uses: per station, `/plan` for each hour of the last 2 hours (train
and planned time) plus one `/fchg` (real time), joined by stop id. IRIS has no per-train
endpoint, so sampling stays per station. The decision below is superseded.


**One `/fchg` call per station is the entire data source.** No per-train or per-trip follow-up
call, before or after — every comparison for a station comes out of that single response.

**The average uses the live app's own lookup, not the bake-off's baseline.** `_delay_pred`
(`web/queries.py`) is the same function the station board already calls; this study calls it
directly rather than recomputing an averages baseline the way `dbrel.ml.baselines` does for
training-time evaluation. This is deliberate: the study is asking "how good is what a user sees
today", not "how good would a from-scratch average be."

**TabM's prediction reuses `DelayModel`/`dbrel.ml.predict`,** the same class `predict-delay` and
the browser endpoint use, loading the current `CURRENT` artifact. No separate inference path.

**Sampling is a uniform random draw over all stations**, not weighted by traffic. Coverage
data shows 5,258 of 5,422 stations already have some stored average and virtually all have
recent history, so a plain random sample needs no filtering for "does this station have
anything to compare against."

**The command is a one-shot pass, run once, not a background poller.** It samples, fetches,
scores and writes a report in a single invocation. Given the network-bound (not memory-heavy)
nature of ~3,000 rate-limited HTTP requests, it can still take several minutes, so per standing
practice it launches through the existing capped-launcher pattern (`dbrel ml launch` or an
equivalent), rather than running inline and unsupervised in a terminal session.

**Implementation detail: route-shape features come from the static timetable, not a live
`/plan` fetch.** A stop's route position (`stop_rank`, `n_stops`, `trip_start_sec`, `direction`)
isn't in the `/fchg` response (see Context); it's looked up via `dbrel.ml.timetable.calls_at` —
resolving the live stop's train identity (`train_identity_key`, from its category+number) to the
local `Train` row, then reading its `StationStop` pattern for that station — instead of fetching
each trip's `/plan`. This costs zero extra API calls: the study still makes exactly one live
call per station. If the train has no local `Train`/`StationStop` match (never imported), that
stop is skipped and counted, never given a guessed route shape. Alternative considered: fetch
each realized trip's `/plan` for exact same-day route shape; rejected as an unneeded second call
per trip when the static pattern already matches what `predict.py`'s own live path uses.

## Risks / Trade-offs

- **The realized-stop count per station is out of our control.** `/fchg`'s retention window is
  IRIS's own behavior, not configurable here, so the total number of (station, stop, side)
  comparisons the study ends up with depends on when it's run and how busy the sampled
  stations are. → Mitigation: the report states how many stations yielded zero realized stops
  and the total comparisons actually scored, so the result's statistical weight is visible
  rather than assumed.
- **No local `Train`/`StationStop` match** for a train that was never imported (very rare,
  given the national static import covers essentially the whole schedule). → Mitigation: that
  stop is skipped and counted, never given a guessed route shape; a wrong holiday/weekday/time
  value is never possible either way, since those come from the fresh call itself, not the
  timetable match.
- **Rate limiting**: 3,000 requests through the same token bucket `dbrel collect` uses means
  this competes with any other concurrent use of the same credentials. → Mitigation: it's a
  one-shot run, not continuous, and reuses the existing `TokenBucket`/retry behavior rather
  than adding a second, uncoordinated request path.
- **A station or stop with unusable data must not abort the run.** → Mitigation: covered by the
  spec's "single station's failure does not fail the whole run" requirement; the same skip-and-
  count handling extends to a stop with no local route-shape match.

## Migration Plan

None — this adds a new, isolated command. Nothing existing is deployed, changed, or rolled
back.
