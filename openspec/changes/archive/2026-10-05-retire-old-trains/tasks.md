# Tasks

## 1. Server and frontend

- [x] 1.1 `train_route` returns `retired: {last_seen_date}` and no stops for a retired train
  (design D1). `suggest` drops retired trains in SQL before its limit. Verify with tests:
  - a retired train's route has the message fields and no stops, and touches no schedule rows
  - a running train is unchanged, and a GTFS train (no last-seen date) is never retired
  - a search matching a retired and a running train lists only the running one
- [x] 1.2 The v2 train page shows the retired message (de/en, date in the locale's format) in place
  of the stop table. Verify with `tsc --noEmit` and `npm run build`.

## 2. Deployed database

- [x] 2.1 The build leaves out retired trains' schedule, run-summary and alias rows, and leaves the
  13 unread train columns NULL (design D2). The checks follow design D3. Verify with fixture tests:
  - a retired fixture train keeps its train row and has no schedule, summary or alias rows
  - the unread columns are NULL
  - the checks pass, and a deliberately wrong running-train row is still refused
  - `scripts/compare_deployed_db.py` finds 0 differences, including a retired train's route
- [x] 2.2 Rebuild the real copy (capped, 4G) next to `slim.sqlite`. Record the size, run the parity
  check (adding a retired train and its number search), and on 0 differences make it
  `slim.sqlite`, keeping the 260 MiB file as a backup.
  Result, 2026-10-05: 174 MiB (260 MiB before), 375 s, peak RSS 604 MiB. The build checked 4,130,487
  schedule rows of running trains, 336,736 trains and 260,804 run-summary rows; 187,603 retired
  trains keep only their train row. The parity check (the usual sample plus retired RE 26405 and a
  search for 26405) found 0 differences in 451 requests (255 s, peak RSS 962 MiB). In the Docker
  image under 512 MB every request returned 200 at 142 MiB, with the Berlin Ostkreuz board at
  0.79 s now that the direction fix is in. `slim.sqlite` is the 174 MiB copy; the 260 MiB one is
  kept as `slim-backup-260MiB-all-trains.sqlite`.

