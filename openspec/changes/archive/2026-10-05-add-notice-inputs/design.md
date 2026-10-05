# Design

## Context

See proposal.md for why. The current state that shapes the approach:

- **Features come from shard columns.** `FullHistory` holds monthly memory-mapped shards with
  `station_id`, `train_id`, `line_id` (NO_LINE for a train without one), `day`, `trip_id` and the
  other raw columns. Numeric features are computed on read (`history.numeric(chunk)`), and the
  prepared inputs are written once per setup under a key that hashes the history, rows,
  vocabulary and prep.
- **Artifacts declare their inputs.** A prep whose numeric list differs from `NUMERIC` records it
  as `features_numeric`; NaN becomes 0 plus a `<name>_missing` column whenever training saw a NaN
  (`arrival.prep_layout`, `arrival_inputs`).
- **The server builds the model input.** `web/queries._model_input` runs the shared feature code
  and `arrival_inputs`; the browser only runs the ONNX model. The 35 MB budget counts the model
  files and the manifest, so anything kept in the database costs the browser nothing.
- **Notices are in `tt_message`.** Rows have `trip_id`, `stop_sequence`, `msg_id`, `msg_type`,
  `category`, `ts`, `deleted`. `code` is empty in every row looked at. About 59 M rows. Only
  `msg_type = 'h'` rows carry the "Bauarbeiten" and "Störung" categories.
- **Measured 2026-10-01** (read-only, per service date, 137 MB RSS, 10.5 min for all 332 dates):
  notices per 1,000 trips ranged 56–152 for construction and 91–150 for disruption in every month,
  so no month is short of notices, and arrivals on trips with a notice were late more often (see
  proposal).
- **Live notices are not available far ahead.** The collector's `/fchg` feed covers about the next
  15 hours, so a rate is the only notice-based input that works for a trip planned a week out.
- **Split.** Training 2025-11-03 to 2026-08-22, validation 2026-08-23 to 2026-09-19, test sealed
  (2026-09-20 to 09-26), as derived by the rolling split.

## Goals / Non-Goals

**Goals:**
- Six rate inputs computed identically in training, validation and serving from one stored table.
- No rate for a row may contain information from that row's own month or from later than the
  training range.
- No change to the dataset shards, and no cost to the browser download.

**Non-Goals:**
- Live or upcoming notices, weather, recent per-train delays and the app's range display (all in
  the on-hold `add-known-ahead-delay-inputs`).
- Parsing message text or delay causes; only the two category prefixes are used.
- Early-stopping patience (changed from 3 to 6 in the full-year change).

## Decisions

### D1. What a rate is

For a key (station, line or train) and a set of dates, over eligible arrivals only (the dataset's
own filter):

- `n` = arrivals counted for the key
- `k_works` = of those, arrivals whose trip has at least one non-deleted construction notice
- `k_disruption` = the same for disruption notices

The smoothed rate is `(k + w * parent) / (n + w)` with weight `w = 20`, where the parent is the
rate one level up: train → line → station → all stations. A train without a line uses the station
as its parent. A key with `n = 0` gives NaN. Six inputs: `works_rate_{station,line,train}` and
`disruption_rate_{station,line,train}`.

A notice counts however late it was published. A rate is a historical property of the place or
train, not a statement about one trip, so including same-day (reactive) notices is what makes it
describe how disruption-prone the key is. The leakage rule in D3 is what keeps this safe.

Alternatives considered:
- A single "any notice" rate: loses the difference between planned works and disruption.
- Counts instead of shares: confounded with how often a key appears.
- A notice rate per hour or per direction: more keys, thinner data, for little gain at this
  resolution. Revisit only if the ablation shows value.

### D2. A daily table, built by SQL per service date

A new table `notice_rate_daily` has key (level, key_id, service_date) and holds `n`, `k_works`,
`k_disruption`. Levels are `station`, `line` and `train`. It is built by
`dbrel ml build-notice-rates`, one service date at a time in its own transaction:

- read the date's notices once (distinct `msg_id` per trip, `deleted = 0` versions only decide
  whether a notice is non-deleted), build `trip_id → has works / has disruption` in memory
- read the date's eligible arrivals and add up the three levels
- replace that date's rows

Per-date work keeps memory small (the measurement ran at 137 MB under a 4 GB cap) and a failed or
interrupted build only repeats the dates it had not finished. Rebuilding is idempotent. The
command reads `trip`, `trip_stop` and `tt_message` only, and writes only the new table. It runs
under `dbrel ml launch` with a memory cap, like every heavy job.

The table has 13 M rows (330 dates × the station, line and train keys active that day; the train
level alone is 11 M), measured on 2026-10-01. A daily table, not a monthly one, because the split falls inside August: a month
bucket would mix training and validation days.

Alternatives considered:
- Rates computed from the shards directly: the shards have no notice column, and adding one
  means rebuilding 99.6 M rows and bumping `CODE_VERSION`.
- Rates as a column in the artifact: puts the table in the manifest and the browser download.

### D3. Leave-one-month-out for training rows

For training rows, the rate of a row on service date `d` in month `m` is computed from the
training dates outside `m`. For validation and test rows, and for serving, it is computed from all
training dates.

This removes the row's own trip, and the rest of its month, from its own rates. Without it, a
train's rate would include the notice that exists because that train was already late on that
very run, a small leak of the answer into the input (the more so for trains with few runs).
The cost is a small mismatch: a training row's rate rests on about 10 of 11 months, a
validation row's on all of them. That is the usual price of out-of-fold target encoding, and
cheaper than the alternative.

Implementation: totals per key over all training dates, and per-month totals per key; a month's
fold is `total − month`. Keys are dense integer ids, so the arrays are small (a few MB each).

Alternatives considered:
- Leave-one-trip-out: needs each row's own notice flag, which the shards don't hold.
- Leave-one-day-out: the same train's other days in the same disruption episode would still
  count; a month is a coarser, safer unit.
- Rates from a fixed earlier window (e.g. the first 3 months): wastes most of the data.

### D4. Features, prep and artifact compatibility

- `features.py` gains `NOTICE_RATES = (the six names)` and the full numeric set
  `ARRIVAL_NUMERIC + NOTICE_RATES`. The existing sets stay as they are.
- The rate columns are computed on read by a new `ml/notice_rates.py` class that is built from
  the table and a list of training dates, and given arrays of key ids and days. It is the single
  code path for training, validation, the offline prediction path and `_model_input`.
- NaN from an unseen train or from a train without a line becomes 0 plus `<name>_missing`; training
  sees such rows naturally (trains new in a month have no rate from the other months), so no
  rows are masked artificially.
- Standardising: rates are in [0, 1] and skewed; the existing standard or robust scaling applies
  per trial as for every numeric input.
- The manifest records `notice_rates = {dates, weight, fingerprint}`. The full date list is kept
  (about 3 KB), not just first and last, because stub days are excluded from training and a range
  could not rebuild the same set. The fingerprint is a hash of the table's counts over those dates. Loading an artifact that
  uses the rates recomputes it and refuses on mismatch (spec: "Changed rate data is refused").
- The prepared-set key already hashes the prep and vocabulary; it also hashes the rates
  fingerprint, so changed rates rebuild the prepared inputs instead of silently reusing them.
- **Serving.** `_model_input` builds the rates only when the artifact's `features_numeric`
  contains them. The rate arrays are loaded once per process from the table (a few MB) and the
  per-call work is six array lookups. Older artifacts never read the table.

### D5. Search and report

- `NUMERIC_SETS` becomes `with-notice` (`ARRIVAL_NUMERIC + NOTICE_RATES`, the default for all
  trials), `no-notice-rates` (`ARRIVAL_NUMERIC`) and `no-doy` (`NUMERIC + NOTICE_RATES`).
- The compact track's variant step trains two variants after the fixed trials, the best network
  with `no-doy` and with `no-notice-rates`. The large track's loose-pruning variant is unchanged.
  Total compact time grows by one trial, about 15–25 min.
- The ranking and the final report list both ablations' validation average error beside the
  winner's, so the notice effect is a number, not a belief.
- The histogram trial ignores numeric inputs and is unaffected.

### D6. Order of work and the real-data steps

The unit tests and the fixture run need no real data. The real-data steps each need the user's
go-ahead: building the table (about 20 min, 4 GB cap), then the compact memory check, the
dry run and the track. The prepared inputs rebuild by themselves because their key changes (about
15 minutes of preparation per set, about 16 GB each on disk).

## Risks / Trade-offs

- **[Risk] The gain is small.** The embeddings already learn typical delay per station and train,
  and part of the notice effect may be inside that. → The `no-notice-rates` ablation measures it;
  if it adds nothing the default set can drop back without touching anything else.
- **[Risk] Reactive notices make a rate a proxy for delay.** A line with a lot of disruption
  notices is a line with delays; that is the point, but the rate also contains the effect of the
  row's own period. → Leave-one-month-out (D3) and the training-dates-only rule for validation,
  test and serving; the spec scenarios test both.
- **[Risk] Notices may be recorded unevenly over the months** (archive replay versus live polls).
  → The 2026-10-01 measurement showed 56–152 construction and 91–150 disruption notices per 1,000
  trips with no month near zero; the builder logs notices per 1,000 trips per month, and a month
  more than 3× below the median is flagged in the build output.
- **[Risk] The table drifts after training** (re-running the builder on changed data). → The
  fingerprint in the manifest makes serving refuse rather than mismatch silently.
- **[Trade-off] Training rows rest on 10 months of rates, validation rows on 11.** Accepted; it
  is the price of not leaking the row's own month.
- **[Risk] Disk.** Each prepared set (fit + eval) grows from about 12 GB to an estimated 16 GB
  with the extra columns. The compact track writes four (standard and robust scaling, plus a
  set per ablation, about 60 GB). The large track shares the compact track's standard and robust
  sets, since a set's name carries no track, and adds only its loose-pruning variant (about
  16 GB), so both tracks need about 75 GB of the 179 GB free on 2026-10-01. The 12 GB of old
  `with-doy` sets are on top and nothing will read them again.
  → Check `df` before each track; delete the old sets, and a finished track's prepared sets,
  with the user's go-ahead. Sharing one set between numeric variants by column selection would
  cut this but is a separate change.
- **[Risk] The first prediction after a server restart loads the rate table** (13 M rows; measured 22 s and 575 MiB at peak) into
  memory. → Measure the load time and resident size in task 6.1; if it is too slow, store the
  summed counts as a compact file next to the manifest.
- **[Trade-off] The compact track gains one trial** (the second ablation), about 15–25 minutes.

## Migration Plan

1. `create_all()` creates `notice_rate_daily`; nothing existing changes.
2. Run `dbrel ml build-notice-rates` on the real database (capped job, about 20 minutes).
3. Rerun the compact memory check (more numeric columns), then the full-year change's tasks 5.2
   and 5.3 with the new default set.
4. Serving picks up the rates only through a new artifact that asks for them. Rollback is
   `promote` back to `20260924T183944Z`, as before; the table can stay.

## Open Questions

- The smoothing weight (20) and the six-input layout are first choices. They are recorded in the
  manifest, so changing them later needs no redesign.
