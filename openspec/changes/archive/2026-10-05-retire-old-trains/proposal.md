# Proposal

## Why

A numbered train that has not appeared in DB's timetable for more than 14 days before the newest
data is *retired*. Boards, line pages and the earlier/later runs already hide such trains. Two
paths still lead to them: a train-number search, and an old link to the train's page. Both show a
route that no longer runs. On 2026-10-05 this was 187,603 of the 250,194 timetable trains. Train
numbers change at timetable changes, during construction work and for one-off trains, and the
service usually continues under a new number.

In the deployed database, retired trains hold 39% of the route variants, 20% of the stored pattern
stops, 48% of the run-summary rows and 75% of the search aliases. Thirteen `train` columns are
never read by the server or the frontend (13.4 MB of them `long_name`).

## What Changes

- **A retired train's page says so.** It shows "This train hasn't run since <date>. It may now run
  under a new number." and no stops. This is decided by the server, the same way on both
  databases.
- **Search does not offer retired trains.** Stations, lines and running trains are found as
  before.
- **The deployed database leaves out what only retired trains need:** their schedule rows, their
  run-summary rows and their search aliases. Every train keeps its row, because directions and
  corridors are computed from all of a line's trains. The 13 columns nothing reads are left empty.
- **The build's checks follow suit.** Schedule and run-summary rows are compared for running
  trains. Train rows are compared in every column the server reads, and the build checks that the
  unread ones are empty. The parity check must still find no difference.

Out of scope: the definition of "retired" (unchanged, the boards' 14-day rule), and directions
(still computed from every train of a line).

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `deployed-database`: it holds no schedule, run summary or search aliases for retired trains, and
  no values for unread train columns. Exact reproduction applies to the rows the server can still
  reach.
- `web-frontend-v2`: a retired train's page shows a "no longer running" message, and search does
  not offer retired trains.

## Impact

- **Code:**
  - `web/queries.py` (`train_route`, `suggest`)
  - the v2 train page and `i18n.ts`
  - `dbrel.deployed`, `dbrel.deployed_build`
  - tests
- **API:** `/api/train/<id>/route` adds `retired: {last_seen_date}` for a retired train, with an
  empty `stops`. `/api/suggest` no longer lists retired trains.
- **Data:** the deployed file shrinks further. The estimate is about 85 MB of 260 MiB, and the
  build measures it.
