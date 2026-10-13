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
modulo `n_calls`, so the timetable repeats day after day. Each returned trip carries its
`position` and its `day_offset` (which day it is on, counted from the chosen date: `i // n` for
the picked index `i`). The `count` cap is `MAX_WINDOW_SIDE`. `before`/`after` keep working, so v1
is untouched. Internally both forms become one list of positions, so the picking code stays a
single path. Only `before`/`after` requests de-duplicate by `stop.id`; a position request returns
every position, so a stop with 4 runs a day and `count=12` returns 3 days of runs. The model input
of a run is built for the chosen date plus its `day_offset`, so another day's run is forecast for
that day (the stored averages still use the chosen day's day type).

In the frontend, `useRouteTripsAround` is replaced by a chunk hook built on `useQueries`. Each
chunk is its own query, keyed by `(route, direction, station, date, time, offset, count)`, so a
chunk is fetched once and stays cached while the inputs don't change. The hook returns a
`Map<position, TripAround>` and the range loaded so far.

Load order after Prognose:
1. `offset=-2,count=3`, the window shown. Its forecast is its own `useModelPredictions` batch.
2. Only after step 1 has both data and forecasts: `offset=-14,count=12` and `offset=1,count=12`,
   each forecast in its own batch (the model keys forecasts by position).
3. While the window is at `[s, s+2]`, with `lo…hi` loaded: when `hi - (s+2) <= 6`, request
   `offset=hi+1,count=12`. When `s - lo <= 6`, request `offset=lo-12,count=12`.

The hook keeps runs by position (no sharing between positions that are a day apart, since each
has its own date and so its own forecast). Fetching ahead has no end.

*Alternative:* keep `before`/`after` and grow them. That was rejected because every growth step
fetches and forecasts the whole set again, which is the slowness the user reported.

### 4. Window stepping has no limits
The window start `s` begins at -2. Later sets `s' = s + 3`, Earlier sets `s' = s - 3`, with no
clamp: the timetable repeats daily, so a stop with few runs a day just shows its runs again on
the next day, each with that day's date. Buttons are disabled only while a slide is running (and
before the first chunk has loaded). A step whose 3 runs are not loaded yet shows skeleton rows for
the missing positions, keyed by position. The prefetch in decision 3 is there so that this is
rare.

The cassette moves by the number of positions that changed. Slots are keyed by position, not by
run, so a run that appears in two slots (a short day wrapping) is still two slots and the ghosts
are the positions that left the window. Its distance is measured: for Earlier the height of the
rows that came in at the top, for Later the height of the ghost rows.

*Alternative (first built, then dropped):* clamp the window to one pass round the day
(`1 - n … n - 5`). It made Earlier/Later stop at a few-run stop and let a wrapped run appear in
two slots, so the content shifted by more slots than the animation moved.

### 5. Header name, and per-run names only for line-less routes
`savedSearch` stores `selectedLabel`, the `name` of the suggestion the user picked. The card
header shows a train icon, `selectedLabel`, and the stop name with its icon. For
`RouteRef.kind === "line"`, rows show no name and no `TrainNumber`. For `kind === "corridor"`,
a row shows `trip.name` only when it differs from `selectedLabel`, because those runs are
genuinely different trains (ICE 610, 614, …) and labelling them all "ICE 612" would be wrong.

### 6. One run entry: two lines, three columns, one fixed height
An entry is a link (the whole row opens the train page) with a fixed height (`h-[4.25rem]` on the
outer element, so the divider counts inside it) laid out as a grid of three equal columns plus a
chevron. A single column header row above the list (scheduled · delay · expected, each with its
icon; icons hidden on a phone) replaces per-entry labels. Line 1: scheduled time (the departure
for a run that starts here), the delay text, the expected time. Line 2: the day (weekday and
date, always shown), and in the delay/expected columns the notes: "starts here" or "ends here"
with their icons, and for a corridor route the run's own name. A run that starts here has no
arrival, so line 1's delay/expected columns hold one sentence ("no arrival, no forecast"). No
entry has an optional extra line, so no case can be taller than another. The board uses the same
layout (see §9).

*Alternatives (first built, then dropped):* labelled cells inside each entry plus a full-width
bordered notice. The notice made the entry taller; reserving room for it left large empty space
in every other entry.

### 7. Icons: one map, lucide only
`components/v2/icons.ts` maps each kind to one lucide icon: state → `Map`, search → `Search`,
direction → `ArrowRightLeft` (two arrows the opposite ways), stop/station → `MapPin`, date →
`CalendarDays`, time → `Clock3`, scheduled → `CalendarClock`, delay → `Hourglass`, expected and
arrival → `ArrowRightToLine`, departure → `ArrowRightFromLine`, starts here → `CirclePlay`, ends
here → `Flag`, train → `TrainFront`. The first choice (download and upload arrows for arrival and
departure) read badly and was replaced. Labels render `<Icon/> text` through `FieldLabel`.

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

### 9. Station board entries reuse the Prognose entry
`BoardV2` renders its calls with the same entry component and column header as the Prognose card
(shared in `components/v2`), without the Earlier/Later column. A call has `arr` and `dep`: a call
with no arrival shows "starts here" and its departure; one that terminates shows "ends here".
The board's own columns are the same three. The day (second line) is the board's date, or the
call's `service_date`. The desktop table and the phone list are replaced by this one layout, which
already works at phone width.

### 10. Earlier/Later beside the list; dark text
The result body is a flex row from `sm` up: the list takes the width, the two buttons sit in a
column to its right (`w-36`, a left divider), vertically centred. Below `sm` the buttons are a row
under the list. In the v2 dark theme `--fog` and `--mist` are lightened so labels and secondary
times read comfortably.

### 11. Stop order: merge every run's sequence
`_stations_in_travel_order` used to take the longest run's order and put every other run's stops at
the end. It now collects each run variant's stop sequence, de-duplicates identical ones, and
merges them (`_merge_stop_orders`): the longest first, then each further stop is inserted just
before the stop where its run rejoins the order, or else right after the stop where it left.

*Alternative:* a topological sort of all runs' successor pairs. It breaks on the loops that real
timetables have (a ring line, a run in the other direction in the data), where the merge simply
keeps the first order.

## Risks / Trade-offs

- [Up to 27 runs forecast per Prognose instead of 3–5, and more as the user steps] → They are
  small batches after the first one has rendered, one after another. `predictBatch` runs off the
  main thread.
- [Averages for a run on another day use the chosen day's day type] → The model forecast, which
  is the number shown, uses the run's own date. The averages are not shown on this card.
- [A fixed row height can clip a long note or name] → Notes and names truncate; the whole text is
  in the row's tooltip.
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
