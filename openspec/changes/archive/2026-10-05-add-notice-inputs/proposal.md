# Proposal

## Why

Some places and trains see far more construction and disruption notices than others, and those
trains are late more often. Measured on the database on 2026-10-01 (99.6 M eligible arrivals,
2025-11 to 2026-09, every month): 15–18% of arrivals were more than 5 minutes late with no notice
on their trip, 17–25% with a known construction notice and 18–31% with a known disruption notice.
The arrival model for the full-year change (`rebuild-arrival-model-for-full-year`) has no input
that says how notice-prone a station, line or train is, so it can only learn that indirectly
through its embeddings, and not at all for rarely seen trains.

Prediction must not depend on live data: the app makes no API calls for a forecast, and users plan
trips at any lead time. So the input has to be a stored historical rate, not a lookup of today's
notices (DB's live feed only covers the next ~15 hours, so a week-ahead trip has no notices to
look up).

## What Changes

- **Notice-rate inputs.** Six numeric inputs per arrival: the share of past arrivals whose trip had
  a construction notice ("Bauarbeiten") and the share that had a disruption notice ("Störung",
  "Großstörung"), each at the station, the line and the train. They are historical rates from the
  database's `tt_message` table, smoothed toward the next level up so a rarely seen key stays
  stable, and missing (never zero) for a key with no history.
- **No leakage of the answer.** A rate for a training row is computed from the other months of
  the training dates, never from its own month. Rates for validation and test rows, and for
  serving, come from the training dates only. A notice written because a train was already late
  therefore cannot reach the row it belongs to.
- **Stored, not fetched.** A new table holds per-day notice counts per station, line and train,
  built by a new command from existing data. Prediction reads stored rates only: no API call, no
  live notice lookup. The artifact records which dates its rates come from, so training and
  serving use the same table.
- **The full-year search measures the inputs.** The default numeric set includes the six rates;
  the compact track adds a `no-notice-rates` ablation next to `no-doy`, so the report shows what
  the rates add.
- **BREAKING (spec):** the rule "no feature SHALL come from … disruption messages" becomes
  "no feature SHALL come from the run's own messages or from any other run on the same service
  date"; historical notice rates from other dates are allowed. Deployed artifacts keep working
  unchanged because they declare their own input lists.

Out of scope: live or upcoming notices as inputs, weather, recent per-train delays and the app's
range display (all in the on-hold `add-known-ahead-delay-inputs`, which is untouched), and the
early-stopping patience (6 instead of 3, set in the full-year change).

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `delay-model`: historical notice rates become allowed inputs under a time-separation rule;
  notice rates are stored, computed identically in training and prediction, and never from the
  row's own month; the search reports an ablation without them.

## Impact

- **Code:**
  - `src/dbrel/models.py` (new `NoticeRateDaily` table) and the schema migration
  - `src/dbrel/derive/` (builder) and `src/dbrel/ml/cli.py` (`build-notice-rates`)
  - new `src/dbrel/ml/notice_rates.py`
  - `src/dbrel/ml/features.py`, `full_history.py`, `full_year.py`, `full_year_search.py`
  - `src/dbrel/web/queries.py` (`_model_input`) and the offline prediction path, for artifacts
    that ask for the rates
- **Database:** one new table, filled from `trip`, `trip_stop` and `tt_message`, which are only
  read. 13 M rows (measured), built in about 20 minutes with 0.3 GiB of memory.
- **Artifacts:** a small `notice_rates` entry in the manifest (dates and smoothing weight), no
  tables in the browser download, so the 35 MB budget is unaffected.
- **Training:** each prepared set grows from about 12 GB to about 16 GB (eight more numeric
  columns) and is rebuilt because its key changes; the old sets are orphaned. The compact memory
  check is rerun, and the launch gate refuses a check made before this change.
- **Ordering:** this builds on `rebuild-arrival-model-for-full-year`, which also modifies the
  "Training rows use only facts knowable…" requirement. That change archives first; this delta is
  written against its version of the requirement.
