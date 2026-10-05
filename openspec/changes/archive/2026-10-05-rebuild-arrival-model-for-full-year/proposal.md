# Proposal

## Why

The arrival model deployed on 2026-09-26 (a residual MLP with a "densest 6-minute window" point
rule, rolled back on 2026-09-29) showed exactly +3.0 min for 78% of arrivals: it was selected on the share within 3 minutes, which a constant
"about +3" nearly maximises, while within-1-minute accuracy fell from 47% to 24% and average error
rose from 2.97 to 3.56 min. It was also trained on one month (2026-08-24..09-12), while the
archive backfill is now collecting about 11 months (national coverage from 2025-11-01), roughly
130 million recorded arrivals instead of 12.5 million. The current pipeline cannot use that
data: it has no notion of time of year, and building, loading and training would exceed the
machine's RAM, the 14 GiB job cap and the RX 9070 XT's 2 GiB-per-tensor ROCm limit.

## What Changes

- **Point estimate and selection metric.** The shown arrival estimate is the predicted median.
  Candidates, early stopping and the final choice are judged primarily by average absolute error
  against individual recorded arrivals; within 1/3/5 minutes stay as reported secondary scores.
  The window point rule is no longer a deployable decision rule.
- **Time-of-year feature.** Add the service date's position in the year (day and month only, no
  year) as a cyclic feature, computed by the shared feature code for training and prediction
  alike. Reports break accuracy down by calendar month.
- **Full-history dataset.** Rebuild the arrival dataset so it can hold every recorded arrival the
  database has (target: at least 12 months of national data) within the job memory cap: integer
  encoding while reading, per-column files read on demand, no whole-dataset Python objects.
- **Data-driven splits.** Replace the hard-coded date lists with rules derived from the dataset's
  dates: a trailing validation window, a later sealed test window, everything earlier for
  training; the five-hour cutoff rule is kept.
- **Streaming training.** Training data stays in host memory and reaches the GPU one batch at a
  time; no GPU tensor, including input data, may reach 2 GiB.
- **Train vocabulary pruning.** Trains with too little or too old training history encode as
  UNKNOWN, so the train table stays bounded as the timetable changes over a year.
- **Size budget.** The browser assets stay within the existing 35 MB limit; export refuses larger
  artifacts.
- **Browser support for arrival-only percentile output.** The frontend runs an arrival-only model
  that outputs p25/p50/p75 directly and shows its median.
- **A second, large comparison model (≤ 100 MB).** Trained on the same data, split and rules as the
  deployable model, with a bigger network. It is exported and scored in the same reports and is
  usable from the CLI. It is never served to the browser and can never become `CURRENT`.
- **The old two-sided TabM is the incumbent and a permanent reference.** On 2026-09-29, `CURRENT`
  was moved back from the window MLP (`20260926T113337Z`) to the two-sided TabM
  (`20260924T183944Z`) because of the +3.0 problem. The TabM stays the incumbent until an approved
  replacement. Every report scores it on the same arrivals, before and after deployment.
- **On approved deployment only:** the compact median model replaces the TabM as `CURRENT`. Both
  earlier artifacts stay on disk for rollback and comparison. `CURRENT` does not move without
  approval.

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `delay-model`: time-of-year feature; median point estimate with average absolute error as the
  primary selection score; per-month reporting; full-history dataset within the memory cap;
  data-driven date splits; GPU input tensors under the 2 GiB limit; bounded train vocabulary; a
  non-deployable large comparison model; the old TabM as a permanent comparison reference.

## Impact

- **Code:** `src/dbrel/ml/{dataset,features,split,arrival,arrival_nn,arrival_runs,metrics,timetable}.py`,
  `scripts/{search_arrival,final_arrival,export_arrival_artifact,probe_arrival}.py`,
  `src/dbrel/web/queries.py` (model input gains the date feature), and
  `bahn-buddy-predict/src/lib/tabm.ts` (arrival-only percentile output); their tests.
- **Data:** read-only on the database and averages. A new dataset cache is written alongside the
  existing `data/ml/arrival_dataset.npz`, which is preserved. Needs the archive backfill to have
  run over the intended window first.
- **Artifacts:**
  - A new deployable artifact goes under `data/models/v2/`.
  - The large comparison artifact goes under a separate `data/models/v2-comparison/`, which the
    web server never reads.
  - `20260924T183944Z` (two-sided TabM, `CURRENT` since 2026-09-29) and `20260926T113337Z`
    (window MLP) stay.
- **CLI:** `dbrel ml predict-delay` gains an option to predict with a specific artifact, such as
  the large comparison model.
- **Live accuracy study:** labels the estimate as a median again; no other change expected.
- **Compute:** a full search takes hours rather than minutes. The large track roughly doubles
  it. Both run as capped `dbrel ml launch` jobs, checkpointed per trial as today.
