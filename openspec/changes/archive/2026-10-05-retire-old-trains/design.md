# Design

## Context

- The server's rule is `_is_active(train, cutoff)`. The cutoff is the newest `last_seen_date`
  minus `ACTIVE_WINDOW_DAYS` (14). GTFS trains have no `last_seen_date` and are always active.
  Boards, line views and runs-around use it; `train_route` and `suggest` do not.
- `direction_by_train_id` and the corridor grouping read every train of a line or category,
  retired ones included. Their routes therefore decide active trains' directions and must stay.

## Decisions

### D1. The server decides, the same way on both databases
`train_route` checks `_is_active` first. For a retired train it returns the usual `train` header,
`retired: {"last_seen_date": ...}`, `stops: []`, `n_monitored: 0` and the model stamp, and touches
no schedule rows. `suggest` joins aliases to their train and drops retired ones in SQL, before the
result limit, so retired matches cannot crowd out running ones. The two databases give identical
answers because their train rows, and so their cutoffs, are identical.

### D2. What the deployed build leaves out
The build computes the same cutoff from the source's trains. For retired trains it writes no
`train_variant`, `variant_id_run` or `variant_run_data` rows, so patterns used only by them are
never created. It also copies no `search_alias` rows pointing at them. Every `train_data` row
stays. These columns are written as NULL for every train: `dest_station_id`, `first_seen_date`,
`first_seen_feed_version_id`, `identity_kind`, `last_arrival_sec`, `last_seen_feed_version_id`,
`long_name`, `n_runs_seen`, `n_stops_tracked`, `passenger_label`, `route_id`, `train_number_raw`
and `trip_type`. A 2026-10-05 search found no reference to any of them in the server, the model
code or the frontend.

### D3. Verification
- Schedule rows and run-summary rows are compared for running trains only.
- Train rows are compared in every other column, and the unread ones must be NULL.
- The parity check on the usual sample must find 0 differences. It now also requests a retired
  train's route and a search for its number.

## Risks / Trade-offs

- **[Risk] A column is read somewhere the search missed.** → The parity check compares whole
  responses, the model inputs included.
- **[Trade-off] A train that comes back after being retired** is missing from a deployed copy until
  the next build. The copy is rebuilt for every deployment.
