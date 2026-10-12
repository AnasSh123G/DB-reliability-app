# Tasks

## 1. Prerequisite

- [ ] 1.1 Confirm with the user that the uncommitted Bundesland filter (`lib/states.ts`, `tests/test_search_state.py`, the `state` param of `/api/suggest`) is committed first, as its own commit; verify `git status` shows none of those files modified before starting group 2

## 2. Backend: board at a chosen date and time

- [ ] 2.1 In `src/dbrel/web/views.py` `station_board`, read optional `date` (YYYY-MM-DD) and `time` (HH:MM); both or neither, else 400; build the epoch in `Europe/Berlin` with `zoneinfo` (`fold=0`) and pass it as `now=`; return 400 naming the bad value when unparsable; verify with `curl 'localhost:8000/api/station/<id>/board?date=2026-10-20&time=07:30'` that `window.date` is 2026-10-20 and `window.from`/`to` surround 07:30
- [ ] 2.2 Add tests to `tests/test_station_board.py` (or `tests/test_board_window.py`): a chosen moment lists the calls around it and not around now; date without time is 400; a bad date is 400; 2026-10-25 02:30 resolves without error; verify `pytest tests/test_station_board.py tests/test_board_window.py` passes

## 3. Backend: trips-around by position

- [ ] 3.1 In `queries._trips_around_in` (and the `trips_around`/`corridor_trips_around` signatures), accept `offset`/`count` as an alternative to `n_before`/`n_after`, turn both into one list of positions relative to `after_idx`, keep the modulo-`n` wrap and the per-response dedup, and add `"position"` to each trip; verify the existing tests in `tests/test_corridor.py` and `tests/test_browse.py` still pass
- [ ] 3.2 In `views._trips_around_response`, read `offset` (signed int) and `count` (1…`MAX_WINDOW_SIDE`); when `offset` is present it wins over `before`/`after`; 400 on non-integers; verify with curl that `offset=-2&count=3` returns the same runs as the default `before=2&after=1`, with positions -2, -1, 0
- [ ] 3.3 Add tests: positions are consecutive and wrap past midnight; `offset=1,count=12` followed by `offset=13,count=12` has no overlap when `n_calls ≥ 24`; with `n_calls = 4`, `count=12` returns 4 distinct runs; `before`/`after` behave as before; verify `pytest -k around` passes

## 4. Frontend plumbing

- [ ] 4.1 In `lib/api.ts`, add `position` to `TripAround`, a `routeTripsChunk(ref, direction, stationId, date, time, offset, count)` call, and optional `date`/`time` on `stationBoard`; keep `routeTripsAround` for v1; verify `npx tsc --noEmit` in `bahn-buddy-predict` is clean
- [ ] 4.2 In `hooks/use-dbrel.ts`, add `useTripChunks` (one `useQueries` entry per chunk, keyed by route, direction, station, date, time, offset and count), returning a `Map<position, TripAround>`, the loaded `lo…hi`, `n_calls`, and a `request(offset, count)` to add a chunk; give `useStationBoard` `date`/`time` and turn off `refetchInterval` when they are set; verify `npx tsc --noEmit` is clean
- [ ] 4.3 Add the new DE and EN strings to `lib/i18n.ts`: starts-here and ends-here notices, scheduled arrival, delay, expected arrival, "+{n} min", show board, your stop, departure label; verify both locales have the same keys (`npx tsc --noEmit` catches a missing key through the `Copy` type)

## 5. Shared v2 pieces: icons, labels, delay text, notices

- [ ] 5.1 Create `components/v2/icons.ts` with the mapping from each kind of field or value to its lucide icon (design §7), and a `FieldLabel` component that renders the icon and the text; verify that `npm run build` succeeds
- [ ] 5.2 Give `ModelDelta.tsx` a compact mode that returns signed text ("+4 min" / "on time") with a tone icon (check, clock or alert), so colour is never the only signal; verify in the browser, in greyscale (DevTools rendering → emulate achromatopsia), that on-time, amber and red runs can be told apart
- [ ] 5.3 Add a `RunNotice` component for starts here and ends here (full-width bordered block, icon and sentence; starts here also shows the departure time); verify it renders in both locales and both themes

## 6. Search view: station flow

- [ ] 6.1 In `SearchV2.tsx`, add `selectedStation` and `selectedLabel` to the state and to `savedSearch`; a station suggestion sets the station, clears line, direction and stop, and does not navigate; typing or choosing a line clears the station; direction and stop are `disabled` while a station is chosen; verify by hand that choosing "Aachen Hbf" keeps you on `/` with direction and stop greyed out
- [ ] 6.2 Set `canSubmit` to `date && time` for a station; the button reads "Tafel anzeigen" / "Show board" and navigates to `/station/<id>?date=…&time=…`; verify that the board opens for that moment and that coming back restores the form
- [ ] 6.3 In `routes/station/$stationId.tsx`, add `validateSearch` for `date` and `time` and pass them to `BoardV2`; `BoardV2` passes them to `useStationBoard`; verify that a board with date and time does not refetch every 60 s (network panel), that one without them still does, and that "±240 min" widens around the chosen moment

## 7. Search view: Prognose card

- [ ] 7.1 Replace `useRouteTripsAround`/`shift` in `SearchV2` with `useTripChunks` and a window start `s` (initially -2); load `offset=-2,count=3` first, then `-14/12` and `1/12` once the first chunk has data and forecasts; forecast each chunk in its own `useModelPredictions` batch; verify in the network panel that one 3-run request comes first, then two 12-run requests
- [ ] 7.2 Implement stepping and limits on positions (design §4): Later sets `s' = min(s+3, n-5)`, Earlier sets `s' = max(s-3, 1-n)`, buttons are disabled at the limits, while sliding, and when `n < 3`; prefetch the next 12 when the window is within 6 of the loaded edge; stop prefetching once the loaded span reaches `n_calls`; verify that two Later presses trigger exactly one new 12-run request and that a stop with 4 runs allows one Later step of 1 run
- [ ] 7.3 Make the cassette move by `s' - s` rows, with up to 3 ghost rows on the side they left from; show skeleton rows for positions that have not loaded yet; verify that the slide stays smooth and all rows keep the same height at desktop and phone width
- [ ] 7.4 Card header: train icon, `selectedLabel`, and the stop with its icon; for `kind === "line"` the rows show no name and no `TrainNumber`; for `kind === "corridor"` a row shows `trip.name` only when it differs from `selectedLabel`; verify with an RE line and with an ICE route
- [ ] 7.5 Rebuild the run row with fixed labelled slots (relation, scheduled arrival, delay, expected arrival, open route) using `FieldLabel`, the compact `ModelDelta` and `RunNotice` (starts here replaces the three slots, ends here adds a notice line); verify with a stop where a run starts (a line's first stop) and one where a run ends (its last stop)
- [ ] 7.6 Put icons on every search-form label (state, search, direction, stop, date, time) through `FieldLabel`; verify by eye at phone and desktop width

## 8. Shell, width, board, train page

- [ ] 8.1 In `ShellV2.tsx`, merge the toggles into the header row (`flex items-center justify-between`, logo on the left, toggles on the right); verify they stay on one centred row at 360 px and at desktop width
- [ ] 8.2 Remove `max-w-3xl mx-auto` from the search hint, the Prognose card and the train route (`routes/train/$trainId.tsx`), and check that `LineV2` and `BoardV2` fill the same width; verify that all cards share the same left and right edges on `/`, `/train/…`, `/station/…` and `/line/…`
- [ ] 8.3 In `BoardV2.tsx`, add icons to the column labels, `RunNotice` for calls that start here (`arr === null && dep !== null`) and end here (`terminates`), and the compact `ModelDelta`; verify on a terminus station's board
- [ ] 8.4 In `TrainV2.tsx`, highlight rows whose `station_id === stationId` (teal left border and tint, filled pin, "Deine Haltestelle" / "Your stop" tag) on both the desktop table and the phone list, and scroll the row into view once on load; add icons to the column labels; verify that opening a run from Prognose highlights that stop and that `/train/<id>` without `?station` highlights nothing

## 9. Integration check

- [ ] 9.1 Run `npm run lint`, `npx tsc --noEmit` and `npm run build` in `bahn-buddy-predict`, and `pytest` for the backend; verify that all pass
- [ ] 9.2 On the local server (not on Fly), walk through every scenario in `specs/web-frontend/spec.md` and `specs/web-frontend-v2/spec.md` in DE and EN, light and dark, desktop and phone; verify that `/v1` looks and behaves as before; report the results to the user and do not commit or push until they ask
