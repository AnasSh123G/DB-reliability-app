# Tasks

## 1. Backend: expose real delay averages and missing fields via the JSON API

- [x] 1.1 In `queries.py`, replace the hardcoded `dep_pred`/`pred` `None` values (in
      `station_board()`, `train_route()`, `trips_around()`, `line_detail()`) with a lookup
      against `stop_delay_average`, keyed by (station, line, direction, trip start time,
      scheduled time, `is_holiday`); when no row exists or the relevant side (`n_arr`/`n_dep`)
      is zero, keep the field `None` rather than fabricating a value. Verify by querying a
      station/line/time combination known to have averages (from the archived
      `add-holiday-aware-stop-averages` work) and confirming a non-null `p50_min` comes back,
      and a combination with no rows still returns `None`.
- [x] 1.2 Audit each JSON endpoint's response shape against every field the `web-frontend` spec
      requires (platform, origin/destination names, `n_stops`/`n_stops_tracked`,
      replacement-bus flag, cancellation state, monitored-vs-unmonitored per stop) and add any
      field that's missing. Verify by listing, per endpoint, the spec requirement each field
      satisfies.
- [x] 1.3 Fill out (or add) a JSON line-detail endpoint carrying everything `line.html`/
      `_directions.html` render today (both directions' runs, replacement-bus warning, "this
      run"/"part of route" tags), matching what `line_detail()` already computes for the
      server-rendered page. Verify with a request against a known multi-direction line and a
      known replacement-bus line (S9 or S46).

## 2. Frontend: remove mock data and wire a real API client

- [x] 2.1 Delete `bahn-buddy-predict/src/lib/trains.ts` and every symbol it exported
      (`TRAIN_SERVICES`, `getStopDelay`, `getTrainDirections`, `searchTrains`,
      `getTrainService`, `minutesToTime`/`timeToMinutes`). Verify with a repo-wide search
      confirming no remaining import of `lib/trains`.
- [x] 2.2 Add a typed API client (fetch wrappers) for the `dbrel` JSON endpoints and wire
      `@tanstack/react-query` hooks (`useQuery`) for suggest, station board, train route, line
      directions/stations, and trips-around. Verify by confirming each hook's inferred response
      type matches the corresponding endpoint's actual JSON shape from task 1.
- [x] 2.3 Decide and implement the base API URL configuration (e.g. same-origin relative paths
      once served by Django, per design.md decision 6) and verify a request succeeds against a
      locally running `dbrel` server.

## 3. Frontend: rebuild the search and train-route views against real data

- [x] 3.1 Rewrite `TrainSearch.tsx`'s line/train autocomplete to call the suggest endpoint
      instead of filtering `TRAIN_SERVICES`. Verify by typing a real line/train number present
      in the local database and confirming matching suggestions appear.
- [x] 3.2 Rewrite direction selection to use the line's real directions (from the
      line-directions/line-stations endpoints) instead of `getTrainDirections()`'s fabricated
      reverse leg. Verify both directions of a real line show their own real stop lists.
- [x] 3.3 Rewrite the "services around a time" panel to call `trips_around` instead of
      generating a fixed 120-minute-spaced synthetic sequence. Verify the results match what
      `trips_around()` returns for the same station/line/direction/time via a direct query.
- [x] 3.4 Rewrite `TrainOverview.tsx`'s per-stop table to source scheduled times, platform, and
      expected arrival/departure from the train-route endpoint's real data (including the
      `dep_pred`/`pred` figures from task 1.1), and to render the "no average available" empty
      state per the `web-frontend` spec when a stop has no average. Verify against a real train
      with a known-tracked stop (shows times) and a known-untracked stop on its path (shows
      "not monitored", no fabricated time).

## 4. Frontend: build the station board and line detail views

- [x] 4.1 Add a station board component matching `station.html`'s behavior (calls within a time
      window, platform, delay averages, "ends here"/direction labeling, untracked-station
      warning), built from the template's existing shadcn primitives and palette tokens.
      Verify against a real tracked station and a real untracked station.
- [x] 4.2 Add a line detail component matching `line.html`'s behavior (both directions' runs,
      replacement-bus warning, "this run"/"part of route" tags), built the same way. Verify
      against a real multi-direction line and the known S9/S46 replacement-bus lines.
- [x] 4.3 Extend `src/lib/i18n.ts`'s `copy` dictionary with DE/EN labels for the new views'
      strings, following the existing flat-key convention. Verify both views render fully in
      both locales with no missing-key fallback text.

## 5. Frontend: real routes in place of the single toggle-state page

- [x] 5.1 Add TanStack Router file-based routes for `/`, `/station/$stationId`,
      `/train/$trainId`, and `/line/$lineId`, replacing the `selectedTrain`-driven toggle in
      `routes/index.tsx`. Verify each URL loads its corresponding view directly (e.g. reloading
      `/train/<id>` renders that train, not the search screen).
- [x] 5.2 Wire in-app navigation (search results, "show all stops", station/line links) to use
      real route links instead of local component state. Verify clicking through
      search → train → station → line and back preserves correct browser history/back-button
      behavior.

## 6. Build, serve, and cut over from the Django UI

- [x] 6.1 Set `nitro: { preset: "node" }` in `vite.config.ts` in place of the Cloudflare default,
      per design.md's revised decision 6 (the `tanstackStart.spa` static-shell path was tried
      first and found broken in this toolchain version — see design.md). Verify `vite build`
      produces `.output/server/index.mjs` and `.output/public/` with no Cloudflare-specific
      output, and that running `node .output/server/index.mjs` directly serves a real rendered
      page on request.
- [x] 6.2 Extend `dbrel serve` (`cli.py`) to spawn `.output/server/index.mjs` as a subprocess on
      a fixed local port and terminate it on exit, and add a catch-all view in `urls.py` (after
      every `/api/*` pattern) that proxies any other request to that process via `httpx`, per
      design.md's corrected decision 6 — there is no existing reverse proxy to extend; `dbrel
      serve` is the project's single local command/port. Verify all four routes load correctly
      through the one `dbrel serve` port, and that the proxy returns a clear 502 (not a hang or a
      bare 500) if the Node process isn't up yet.
- [x] 6.3 Remove `views.index`/`station`/`train`/`line`, the `search.html`/`station.html`/
      `train.html`/`line.html`/`_directions.html` templates, and `static/dbrel/{app.css,app.js,
      browse.js}`, once 6.2 is verified. Verify `urls.py` no longer references the deleted view
      functions and the app still starts.

## 7. End-to-end verification

- [x] 7.1 Manually exercised, through `dbrel serve` end-to-end (real backend + built frontend,
      browser-driven), against the live local `dbrel.sqlite`: search "RE1" → real line
      suggestions → direction/stop pickers populated from real `line_directions`/`line_stations`
      → real `trips_around` results → train route for NX 26811/RB 10308 (line 1/RB33), showing
      both a monitored stop with real arrival+departure averages (after fixing a coalesced-
      sched_time bug caught during this pass -- see `test_delay_pred.py`) and unmonitored stops
      correctly marked with no fabricated times → station board for Aachen Hbf (id 1, tracked) →
      line detail for RE1 (id 9, 290 runs) and both known replacement-bus lines S9/S46 (ids
      158/159, `replacement_bus: true`). No console/page errors in any of it. **Caveat**: this
      installation currently tracks all stations nationally (0 untracked stations exist in the
      live database), so the "untracked station" station-board path could only be verified via
      the code path and existing unit tests, not against live data.
