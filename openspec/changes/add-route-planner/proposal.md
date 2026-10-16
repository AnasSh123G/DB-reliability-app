# Proposal

## Why

BahnPredict can forecast how late one train will be at one stop, but it cannot answer the
question a traveller actually asks: "how do I get from A to B, and when will I really arrive?"
A planner that ranks connections by the model's forecast arrival, not by the timetable,
uses the model for the decision that matters most: whether a connection will be missed.

### What is missing today (found by exploring the code and the deployed database)

1. **No routing code and no endpoint.** Nothing in `src/`, `tests/`, `scripts/` or the frontend
   plans a journey. There is no transfer, journey or connection concept, and the API has no
   route that takes two stations.
2. **No calendar of running days.** The deployed database only knows, per train and route
   variant, the date of its last run and how many runs it had (`train_variant_run`). It cannot
   tell a Monday-to-Friday train from a daily one. A planner built on it would offer weekday
   trains on Sundays. It would also use route variants that stopped running months ago. This is
   the known stale-variant bug, for example S 25145 at Berlin-Wedding, last run 2026-02-22.
3. **No transfer rules.** There are no minimum change times and no walking links between
   stations that are separate stations in the data, like "Berlin Hbf" and "Berlin Hbf (tief)".
4. **The model predicts arrivals only.** No departure delay is predicted, and none may be derived
   from an arrival (delay-model spec). Each stop is forecast on its own, with no live position of
   the train. The only uncertainty is a whole-minute p25/p50/p75 band.
5. **The web server never runs the model.** Today the server builds model inputs, and the
   browser runs the model. A search over thousands of candidate trains cannot ship every input
   to the browser, so the planner has to forecast on the server.
6. **Faulty schedule rows.** In 1,672 route patterns, a stop's time runs backwards or jumps by
   more than 6 hours from the previous stop (1,722 stop pairs). In 81 trains, the times after
   midnight are anchored to the wrong day. A planner must not ride through such a jump.
7. **Frontend gaps.** There is no page, header navigation or station-only picker for a second
   station. The station dropdown is inline code in `SearchV2.tsx`.
8. **Data age.** The deployed schedule ends at the newest collected day (2026-09-28 in the
   current file). Timetable changes after that, such as the December change, are unknown.

## What Changes

- **New planner page at `/plan`.** The user picks a start station, a destination station, a
  date and a time (Today under the date, Now under the time), and presses a button. The station
  fields offer the stations this browser searches most. The page lists up to **3 connections
  whose routes differ**, ranked by **forecast** arrival time, in three labelled columns: the trip
  by the timetable, the forecast arrival, and the chance of missing a change.
- **Chance of missing a change.** Each change gets a band (under 25 %, 25–50 %, 50–75 %, over
  75 %), placed between the model's 25th, 50th and 75th percentile arrival delays. A connection
  shows its worst change.
- **Other times for the same route.** Clicking a connection lists the same route at other times
  of day: the same lines, boarded and left at the same stations, departing earlier and later.
  The list always shows 5 entries in a box of fixed height, reloads in place when another
  connection is chosen, and steps Earlier/Later by 5. One of the times can be opened to see its
  journey details: each train with timetabled and forecast times, and each change with its time
  to change and its miss chance.
- **A\* search on forecast arrival times.** Stations are the nodes and train rides are the
  edges. An edge's weight is the train's **forecast arrival** at the stop where the traveller
  gets off: scheduled arrival plus the model's median delay, never earlier than scheduled. A
  change is possible only if the forecast arrival plus a 5-minute change time is no later than
  the next train's expected departure: scheduled departure plus its own nonnegative forecast
  arrival delay at the change station, capped at 30 minutes (zero for a train starting there
  or without a forecast). Arrival forecasts remain uncapped. The heuristic is the straight-line distance to the
  destination divided by 300 km/h.
- **Three distinct routes.** Routes are told apart by their sequence of (line, boarding station,
  alighting station). The 2nd and 3rd routes are found by searching again with one leg of an
  earlier result forbidden, in the manner of Yen's algorithm.
- **Running-day profile.** A new table stores, per train and route variant, how often it ran on
  each weekday over the newest 28 days of data. On a public holiday, a train counts as running
  if it runs on Sundays. The planner uses only trains whose profile says they run on the chosen
  day, and per train only the variant that ran most on that weekday. The deployed database gets
  this table, so it has to be rebuilt and redeployed.
- **Server-side forecasting for the planner only.** The web process loads the current ONNX
  artifact with onnxruntime to forecast planner searches. Every other page keeps forecasting in
  the browser exactly as now. The planner uses the same artifact and the same input encoding,
  so a call's planner forecast equals its browser forecast.
- **Two new endpoints:** `GET /api/plan` (the up to 3 connections) and `GET /api/plan/timings`
  (one route at other times).
- **Header link** from every v2 page to the planner.

## Capabilities

### New Capabilities

- `route-planner`: what the planner finds and returns. It covers the search, ranking by forecast
  arrival, change rules, running-day filtering, the 3 distinct routes, other times for one
  route, search limits, the two endpoints and the `/plan` page.

### Modified Capabilities

- `web-frontend-v2`: the list of views gains `/plan`. The "behaves exactly like the current
  interface" requirement names the planner as a v2-only difference. The header gets a planner
  link.
- `delay-model`: a new requirement lets the server run the current artifact for planner searches
  only, with inputs identical to the browser path. Every other view keeps browser inference.
- `deployed-database`: the deployed database also holds the weekday running profile and builds it
  from the collection database. The server's planner answers are the same from either database.

## Impact

- **New backend package `src/dbrel/planner/`:** timetable index, running profile, predictor,
  A\* search, alternatives, and the journey and pattern types.
- **Changed backend files:** `src/dbrel/web/queries.py` (two thin functions),
  `src/dbrel/web/views.py`, `src/dbrel/web/urls.py`, `src/dbrel/web/wsgi.py` (warm-up),
  `src/dbrel/deployed.py` and `src/dbrel/deployed_build.py` (profile table),
  `scripts/compare_deployed_db.py` (planner parity).
- **Frontend (`bahn-buddy-predict/src`):**
  - New: `routes/plan.tsx`, `components/v2/StationPicker.tsx`, `PlannerV2.tsx`,
    `JourneyCard.tsx`, `JourneyTimings.tsx`, `JourneyLegs.tsx`.
  - Changed: `lib/api.ts`, `hooks/use-dbrel.ts`, `lib/i18n.ts`, `components/v2/paths.ts`,
    `icons.ts`, `ShellV2.tsx`, and the regenerated `routeTree.gen.ts`.
- **Runtime:** the web process now imports onnxruntime and keeps a compact timetable index
  (about 60–100 MB) in memory. The Fly machine has 1 GB, so the change includes a memory and
  latency measurement, which is reported to the user before anything is deployed.
- **Data:** `data/deploy/slim.sqlite` must be rebuilt with the new table. The old file is kept as
  a backup, as for earlier rebuilds.
- **Order:** archive `improve-v2-search-and-prognose` first (all its tasks are done), because
  this change modifies a `web-frontend-v2` requirement that it also modifies.
- **Not included:** walking links between stations, live delays, departure-delay forecasts,
  platform-aware change times, cancellations, fares, and bus or tram legs (only trains in the
  IRIS timetable).
