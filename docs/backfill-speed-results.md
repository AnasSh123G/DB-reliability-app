# Archive backfill speed-up — results

Change: `openspec/changes/speed-up-archive-backfill/`. Measured 2026-10-18.

## What changed

- Trip updates write straight onto the loaded trip instead of a bulk UPDATE that
  re-scanned the whole session identity map.
- Finalize is bounded to the replayed day and the day before; new indexes
  (`migrate-backfill-indexes`) steer the date lookups.
- Stations, trains and fetch records are loaded once per day, not once per hour.
- Schedule evidence (`record_plans`) runs a fixed number of queries per trip.
- Batched plan writer (`--plan-writer batch`) writes a day's runs together.
- Bulk-load pragmas during backfill.

`--merge-plan-hours` exists but stays off: on the small sample it was faster but not
row-identical to per-hour import.

## Results

Both windows were replayed from fresh seed databases (5,422 tracked stations, no
trips) with the reference code (original code plus the crash fix only) and the new
code (`--plan-writer batch --no-merge-plan-hours`). The resulting databases were
compared table by table with no allow-list: **identical** in both windows.

| Window | Dates | Reference | New | Speedup | Peak memory |
|---|---|---:|---:|---:|---:|
| W1 (DST change) | 2026-03-28 .. 03-30 | 37m31s | 17m21s | 2.16× | 4.3 GiB |
| W3 (archive hole) | 2026-08-27 .. 08-29 | 37m43s | 15m50s | 2.38× | 4.5 GiB |

Per day, the new code takes 4.5–6.8 min against 10.9–14.5 min.

Where the time goes per day (W1 and W3 averages):

| Phase | Reference | New |
|---|---:|---:|
| Plan import | 541 s | 134 s |
| `/fchg` sweeps | 180 s | 168 s |
| Finalize | 30 s | 29 s |

Plan import is 4× faster. The sweeps are now the largest cost and were barely
touched; they are the next thing to optimize.

## Not measured

- **Full-size database.** The original problem was 26m43s for one day replayed into
  the 51 GB live database, where finalize alone took ~9 min because it scanned all
  13.5 M trips. The bounded finalize removes that scan, but no run on a full-size
  copy was done, so the real per-day time there is unknown.
- History independence over a week (WH) and comparison against the live database
  (plan sections 7.5 and 8) were dropped.
