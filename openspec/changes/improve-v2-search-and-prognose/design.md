# Design

## Context

The motivation is in proposal.md. The requirements are in `specs/web-frontend/spec.md` and
`specs/web-frontend-v2/spec.md`. This section covers only the parts of the current code that
shape the approach.

- **Search** (`components/v2/SearchV2.tsx`). A station suggestion calls
  `navigate(v2Paths.station(id))` immediately. Search state lives in module-level `savedSearch`,
  which survives in-app navigation and is reset by a reload; that is deliberate, for hydration.
  The Bundesland filter, uncommitted in the working tree, adds `region` with its own storage.
- **Prognose window.** `useRouteTripsAround(..., shift)` asks for `before = 2 + max(0, -shift)`
  and `after = 1 + max(0, shift)`. Every press of Earlier or Later is therefore one request, and
  it returns every run fetched before, plus one more. The browser forecasts each response's
  runs with `useModelPredictions`, which is keyed by the encoded inputs: one `predictBatch` per
  distinct input list. The backend (`queries._trips_around_in`) sorts all calls at the stop by
  time of day, finds `after_idx` with `bisect`, and picks positions
  `after_idx - n_before … after_idx + n_after - 1` modulo `n`. The view caps each side at
  `MAX_WINDOW_SIDE = 40`.
- **Cassette animation.** The strip is drawn already moved by one row, then slides back. The run
  that left is kept as a "ghost" just outside the clipped window until the slide ends.
- **Station board.** `queries.station_board(session, id, now=…, back_s, fwd_s)` already takes
  `now`. The view never passes it. `board_slices` already handles service days that cross
  midnight and DST days. The frontend board refetches every 60 s.
- **Trip rows** carry `sched_arr`/`sched_dep`: `sched_arr === null` means the run starts at the
  stop, `sched_dep === null` means it ends there. Board calls carry `terminates`, `arr` and
  `dep`. The UI forecasts arrivals only (`.arr`).
- **Train page** (`TrainV2`) already receives `stationId` from `?station=`. Each stop has a
  `station_id`.
- **Widths.** `ShellV2` sets `max-w-5xl`. The search result card, the hint and the train route
  add their own `max-w-3xl mx-auto`. The board adds nothing.

## Goals / Non-Goals

**Goals:**
- Earlier/Later never waits on the network while the next 3 runs are loaded, and the 3 runs shown
  first are forecast before anything else.
- Loading a run never fetches or forecasts runs that are already loaded again.
- One set of shared pieces (icon map, run row, notes, delay text), used by search, board and
  train page, instead of four slightly different copies.

**Non-Goals:**
- No change to v1 under `/v1`, to the model, or to what the backend forecasts. Arrivals only;
  departures are still not forecast.
- The line page gets the shared width and icons, but no other change.
- No virtualised scrolling or infinite list. The card stays a 3-run window with buttons.

## Decisions

### 1. Station flow: a third mode of the same form, not a separate form
`SearchV2` gets `selectedStation: {id, name} | null`, alongside `selectedLine`, and both are kept
in `savedSearch`. Choosing a station sets it, clears line, direction and stop, and closes the
list. Direction and stop are rendered `disabled`, and their values are cleared, not hidden. That
way the layout does not jump and the greyed-out state is plain to see. With a station chosen,
`canSubmit` is `date && time`. The button's label changes to "Tafel anzeigen" / "Show board",
and pressing it navigates to `/station/<id>?date=YYYY-MM-DD&time=HH:MM`. Typing in the search
box clears `selectedStation` the same way it clears `selectedLine` today.

*Alternative:* keep navigating at once and add date and time pickers on the board. That was
rejected because the user asked for the choice to happen on the main page, and the form already
has the date and time inputs.

### 2. Board at a chosen moment: `date` + `time` → `now`, server side
The view `station_board` reads optional `date` (`YYYY-MM-DD`) and `time` (`HH:MM`). Both or
neither must be given; one alone is a 400 error. The view builds a `datetime` in
`Europe/Berlin` with `zoneinfo` (`fold=0`, which on the autumn DST day picks the first 02:30)
and passes its epoch as `now`. Nothing else in `queries.station_board` changes, because it
already resolves day type, slices and model inputs from `now`. An unparsable value returns 400
with a message naming the bad value. In the frontend, the `/station/$stationId` route adds
`validateSearch` for `date` and `time`. `useStationBoard` passes them through, and sets
`refetchInterval` only when they are absent. "±240 min" widens around the chosen moment.

*Alternative:* send an epoch from the browser. That was rejected because the browser's time
zone is not necessarily Berlin, and an address with date and time in it is readable and can be
shared.

### 3. Runs are addressed by position and loaded in chunks
The trips-around endpoint gets two new parameters, `offset` and `count`. Position 0 is the first
run after the chosen time (`after_idx`). Position -1 is the last run at or before it, and so on,
modulo `n_calls` exactly as today. Each returned trip carries its `position`. The `count` cap is
`MAX_WINDOW_SIDE`. `before`/`after` keep working, so v1 is untouched. Internally both forms
become one list of positions, so the picking code stays a single path. The existing per-response
dedup (by `stop.id`) stays, for `n_calls < count`.

In the frontend, `useRouteTripsAround` is replaced by a chunk hook built on `useQueries`. Each
chunk is its own query, keyed by `(route, direction, station, date, time, offset, count)`, so a
chunk is fetched once and stays cached while the inputs don't change. The hook returns a
`Map<position, TripAround>` and the range loaded so far.

Load order after Prognose:
1. `offset=-2,count=3`, the window shown. Its forecast is its own `useModelPredictions` batch.
2. Only after step 1 has both data and forecasts: `offset=-14,count=12` and `offset=1,count=12`,
   each forecast in its own batch.
3. While the window is at `[s, s+2]`, with `lo…hi` loaded: when `hi - (s+2) <= 6`, request
   `offset=hi+1,count=12`. When `s - lo <= 6`, request `offset=lo-12,count=12`.

Requests stop once the span `hi - lo + 1` reaches `n_calls`. Beyond that, positions would only
repeat runs already loaded.

*Alternative:* keep `before`/`after` and grow them. That was rejected because every growth step
fetches and forecasts the whole set again, which is the slowness the user reported.

### 4. Window stepping and limits on positions
The window start `s` begins at -2. Over the whole visit, the window's positions must span at
most `n` = `n_calls` positions together with the initial window -2…0, so no run is shown twice.
That means `s ≥ 1 - n` and `s + 2 ≤ n - 3`. Later sets `s' = min(s + 3, n - 5)`, Earlier sets
`s' = max(s - 3, 1 - n)`. Each button is disabled when `s'` equals `s`, when `n < 3`, or while a
slide is running. With `n < 3` the card shows the runs there are, once each. A step whose 3 runs
are not loaded yet (the network is slower than the user) shows the skeleton rows for the missing
positions. The prefetch in decision 3 is there so that this is rare.

The cassette now moves by `(s' - s)` rows instead of 1. The ghost rows are the runs that left
the window: up to 3, placed outside the window on the side they left from. The existing
`rowHeight` measurement and `SLIDE_MS` stay.

### 5. Header name, and per-run names only for line-less routes
`savedSearch` stores `selectedLabel`, the `name` of the suggestion the user picked. The card
header shows a train icon, `selectedLabel`, and the stop name with its icon. For
`RouteRef.kind === "line"`, rows show no name and no `TrainNumber`. For `kind === "corridor"`,
a row shows `trip.name` only when it differs from `selectedLabel`, because those runs are
genuinely different trains (ICE 610, 614, …) and labelling them all "ICE 612" would be wrong.

### 6. One run row with fixed, labelled slots
A new `RunRow` component lays a run out in a fixed grid: relation tag (Früher/Später) → notes →
three labelled cells (scheduled arrival · delay · expected arrival) → "open route". A run that
starts here replaces the three cells with one notice block: icon, the sentence "Startet hier –
keine Ankunft zu prognostizieren", and the departure time with its label, in the same row
height. A run that ends here keeps the three cells and adds a notice line with its icon. The
notices use the bordered amber style at full width, not a small badge. `ModelDelta` gains a
compact mode that returns a signed text ("+4 min", "pünktlich"/"on time") plus a tone icon
(check / clock / alert-triangle), so colour is never the only signal. The board and train page
use the same `ModelDelta` mode and the same notices.

### 7. Icons: one map, lucide only
A new `components/v2/icons.ts` exports a single mapping from each kind of field or value to its
lucide icon: state → `Map`, search → `Search`, direction → `Signpost`, stop/station → `MapPin`,
date → `CalendarDays`, time → `Clock3`, arrival → `ArrowDownToLine`, departure →
`ArrowUpFromLine`, delay → `Timer`, starts here → `CirclePlay`, ends here → `CircleStop`,
train → `TrainFront`. Labels render `<Icon/> text` through a small `FieldLabel` component. The
app already uses lucide-react, so nothing new is installed.

### 8. Width, logo row, highlight
- **Width.** Remove `max-w-3xl mx-auto` from the search result card, the hint and the train
  route (the `$trainId.tsx` className), so every card fills the shell's `max-w-5xl`.
- **Logo row.** `ShellV2` merges the toggle row into the `<header>` as
  `flex items-center justify-between`, logo and name on the left, toggles on the right. On a
  phone the subtitle may wrap, but the row stays one row.
- **Highlight.** `TrainV2` marks every row whose `station_id === stationId`: a teal left border
  and tinted background, a filled `MapPin`, and a "Deine Haltestelle" / "Your stop" tag, so the
  stop can be read without colour. The page scrolls that row into view once, on load. When the
  train does not call at `stationId`, nothing is highlighted.

## Risks / Trade-offs

- [Up to 27 runs forecast per Prognose instead of 3–5] → They are three small batches after the
  first one has rendered. `predictBatch` runs off the main thread, and positions are capped by
  `n_calls`.
- [Each chunk request still reads every call at the stop server side] → The same work the
  current single request does. The 12-run chunks only add model-input encoding for the runs
  asked for. Measure one hub stop (for example Köln Hbf, RE1) locally before and after.
- [The DST hour does not exist or happens twice] → `zoneinfo` `fold=0` gives a defined answer,
  and `board_slices` already projects across service days. Add a test for 2026-10-25 02:30.
- [Spec drift: v2 "parity with v1" is now a list of exceptions] → The MODIFIED requirement names
  every difference, so a later change has to add to that list explicitly.
- [Uncommitted Bundesland work in the same files] → Land it first, as its own commit, so this
  change's diff stays readable and can be rolled back on its own.

## Migration Plan

1. Commit the Bundesland work that is already in progress.
2. Backend first: the `offset`/`count` and board `date`/`time` parameters are additions, and
   old clients are unaffected.
3. Frontend next. Check everything on the local server: the station flow, a board at a chosen
   time, Earlier/Later stepping and prefetch (watch the network panel), a starts-here and an
   ends-here run, phone and desktop, light and dark, DE and EN.
4. Push only when the user asks. A push to main deploys to Fly. Rollback is reverting the commit.
   No data or schema changes.

## Open Questions

- The exact German and English wording of the two notices and the board button can be tuned
  during implementation without affecting the specs.
