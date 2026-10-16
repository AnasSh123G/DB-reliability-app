# Proposal

## Why

The lookup view (`/`) and the train page (`/train/<id>`) showed the same run in two very
different ways. The lookup used fixed-height entries in scheduled · delay · expected columns,
while the train page used a table. The delays could also differ: the train page forecast every
route for today, whatever day the run was on. Opening a run also meant leaving the list, so
comparing runs took a page change each time.

## What Changes

- **List and route on one page.** Under the search form, the list (the Prognose runs or a
  station board) sits on the left. The chosen run's route sits on the right. Choosing an entry
  shows its route instead of opening the train page. The first run or call at or after the
  chosen time is chosen when the result appears. On phones and tablets the route comes under
  the list, and choosing an entry scrolls to it.
- **Columns.** From 1024 px the list takes 7 parts of the width and the route 5, since the route's
  stops have no chevron. The route's card is exactly as tall as the list's. A station board is
  40rem tall (36rem below 1024 px). Calls and stops that do not fit scroll inside their card, with a
  thin scrollbar in the theme's colours. A board starts at the first call at or after its moment,
  and the route at the user's stop.
- **Six runs.** The Prognose card shows 6 runs at a time (2 before the chosen time, 4 after) and
  steps by 6, so it fills its height. Earlier and Later sit in a column to the left of the runs,
  Earlier on top; on a phone they stay in a row below.
- **The route reads like the list.** Each stop is an entry of the same layout as the list's,
  under the same column header. The entry shows the station's name, the times, the delay and
  the forecast arrival, and under them the departure time and platform. The stop the user came
  from is marked with a bar at its side, a tinted background and its name in teal. There is no
  "your stop" label.
- **A stop opens its board in place.** On the lookup page, choosing a stop of the route replaces
  both cards with that station's board, as wide as both and as tall. The board is for the run's
  day and its time at the stop, with the run the user came from marked and scrolled to. A Back
  button returns to the list and the route as they were.
- **Plain header.** The route's header shows the run's day, the number of stops and a
  "Line RE10" link. It no longer shows the raw category and operator codes (such as
  "RRB · TDRR").
- **Dated route forecast.** `GET /api/train/<id>/route` takes an optional `date` (YYYY-MM-DD,
  else 400). Its stops are forecast for that day. The route beside the list and
  `/train/<id>?date=` pass the run's day, so the route's numbers match the list's.
- **Wider content.** Every v2 view is at most as wide as the larger of 64rem and 55 % of the
  window (was `max-w-5xl`), so the two columns fit.

## Capabilities

### Modified Capabilities

- `web-frontend-v2`: the Prognose card shows and steps by 6 runs, with Earlier and Later on the
  left. The lookup shows the chosen run's route beside the list. The station board's
  entries choose a run instead of opening the train page. The route's stop marker loses its
  label. New requirements cover the equal columns, the stop's board in place, the dated route
  forecast and the plain route header. Like the planner, these are differences only the second
  interface has.

## Impact

- **Backend:** `src/dbrel/web/queries.py` (`train_route` takes `service_date`),
  `src/dbrel/web/views.py` (the `date` parameter) and `tests/test_corridor.py`.
- **Frontend (`bahn-buddy-predict/src`):** `components/v2/SearchV2.tsx`, `BoardV2.tsx`, `hooks/use-run-window.ts`,
  `TrainV2.tsx`, `RunEntry.tsx`, `run-format.ts`, `paths.ts`, `ShellV2.tsx`,
  `routes/train/$trainId.tsx`, `lib/api.ts`, `hooks/use-dbrel.ts` and `lib/i18n.ts`.
- **Order:** archive `add-route-planner` first. Both changes touch `web-frontend-v2`.
- **Not included:** the v1 interface and the line page are unchanged.
