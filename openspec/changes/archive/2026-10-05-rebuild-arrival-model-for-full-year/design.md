# Design

## Context

See proposal.md for motivation. The facts below shape the approach.

- **Data volume.** The current arrival dataset has 12.5 M rows over 36 dates (median 395 k
  recorded arrivals per full day) and is 1.8 GB as one `.npz`. The archive backfill covers
  national data from 2025-11-01 (`ingest/archive.py: NATIONAL_FROM`), so about 11 months means
  about 130 M rows. The SQLite database is 6.3 GB for ~5 weeks, so a year will be ~55–60 GB.
- **Machine.** 30 GB RAM with zram swap. Training jobs run under `dbrel ml launch` with a 14 GiB
  cap. The GPU is an RX 9070 XT (16 GB, ROCm, torch in `data/build/torch-rocm/venv`). ROCm on
  gfx1201 corrupts the tail of any tensor of 2^31 bytes or more.
- **What breaks at scale.**
  - `dataset._build_dataset` keeps all chunks, including five per-row Python-string columns,
    until the end.
  - `search_arrival.py` loads the whole `.npz` and builds `arrival_inputs` for every row, once
    per scaling.
  - `ArrivalNet.fit` copies all training inputs onto the GPU in one tensor each. `safe_batch`
    guards activations only.
  - Dates are fixed in `scripts/probe_arrival.py`.
- **What stays.** The feature code (`features.compute_features`), the model classes
  (`arrival_nn.ArrivalMLP`/`ArrivalTabM`, quantile and binned heads) and ONNX export
  (`save_arrival_artifact`, `promote`) stay. So do the checkpointed trial runner
  (`arrival_runs`), serving (`browser_meta`, `/api/model/<stamp>/`) and the frontend's
  `ModelDelta`, which already labels a `median` rule.
- **Deployed model since 2026-09-29.** `CURRENT` was promoted back from the window MLP
  (`20260926T113337Z`) to the two-sided TabM (`20260924T183944Z`, 20.5 MB). The web server
  reads `CURRENT` on every request and only ever serves that artifact (`views.model_file`
  ignores the stamp in the URL). The frontend's `tabm` branch shows its arrival median; the
  departure output is computed but not displayed.
- **Train vocabulary.** Measured on the current dataset: 42.8 k distinct trains after the first
  week, 57.8 k by 2026-09-11, 64.1 k by 2026-09-26. It still grows by ~400–700 new trains a day,
  and 24% of trains have fewer than 20 arrivals. A year is estimated at 150–250 k trains
  unpruned (extrapolated, not measured).
- **Previous validation results (2026-09-26 search, one month of data).**

  | Candidate | Validation MAE |
  |---|---|
  | Histogram, median rule, `min_support=20` | 2.92 |
  | MLP, quantile head | 2.97 |
  | MLP, binned head, median rule | 3.00 |
  | TabM, quantile head | 3.05 |
  | MLP, binned head, window rule (deployed) | 3.60 |

## Goals / Non-Goals

**Goals:**
- One pipeline that builds, trains, validates and exports from all collected history. Memory
  must not depend on the number of rows beyond the training inputs themselves.
- A time-of-year input the network can use for broad seasonal patterns.
- A deployable median model ≤ 35 MB and a comparison-only median model ≤ 100 MB. Both are
  compared on the same test week against the two-sided TabM (the incumbent) and the window MLP.

**Non-Goals:**
- Recency weighting of training rows, and trend/year features. These are a later experiment
  once this pipeline exists.
- Departure prediction, which stays removed from the UI.
- Changing the archive backfill or database schema.
- Live-input features (upstream delays); the five-hour advance boundary is unchanged.
- Hyperparameter search at the scale of the September bake-off. The trial list here is small
  and fixed.

## Decisions

### D1. Time of year: one sine/cosine pair of a leap-year day index

`compute_features` gains two numeric columns, `doy_sin` and `doy_cos`. They are computed from the
service date's month and day as `idx` = that date's 0-based position in a 366-day (leap) year,
angle = 2π·idx/366. The raw input is a new `RAW_COLUMNS` entry, `day_of_year_idx`. Training
fills it from `trip.service_date` and prediction from `ScheduledCall.service_date`
(`ml/timetable.py`), so both paths share one function.

- Using a fixed leap-year index makes the same month/day identical in every year, as the spec
  requires. The only cost is a one-step skip from 28 Feb to 1 Mar in non-leap years.
- It is one harmonic on purpose. With under a year of data each calendar day appears once, so
  finer encodings let the model memorise one-off events (a line closure in a given week) as
  "seasonal".
- Alternatives considered:
  - Month as a categorical: October, the month about to be predicted, is absent from
    Nov 2025–Sep 2026 and would map to UNKNOWN.
  - Day-of-month as a categorical: links the 13th of every month for no reason.
  - Two or more harmonics: rejected for now. The ablation trial (D7) shows whether even one
    helps.

### D2. Dataset: monthly shards of per-column `.npy`, integer-coded while reading

The new builder writes `data/ml/arrival_full/<build-stamp>/`, alongside `arrival_dataset.npz`,
which is kept.

- It queries one calendar month at a time (by `trip.service_date`). Each month becomes its own
  shard directory with one `.npy` per column.
  - The shard's row count is taken first (`COUNT(*)` with the same filters), so columns can be
    written through `np.lib.format.open_memmap` without holding the month in memory.
  - Text columns (category, operator, state, segment) go through a small append-only
    dictionary per build and are stored as `int16` codes. The dictionary is saved with the
    build.
  - Ids (station, train, line) are stored raw as `int32`. `day` is stored as days since
    1970-01-01 (`int32`) instead of an index into a date list, so shards are independent.
- A build manifest records each shard's row count, dates, rows per date, DB fingerprint and the
  code version. A rebuild after new data only rebuilds the latest (partial) month and any month
  whose count changed.
- The temporary `ml_stop_rank` table moves from `temp_store=MEMORY` to a file-backed temp store,
  because `station_stop` grows with a year of archived plans.
- Loading opens every column with `mmap_mode="r"`. Selecting training/validation rows then
  gathers only what is needed.

Alternatives considered:
- One big `.npz`: it must be decompressed and loaded whole.
- Parquet/Arrow: a new dependency, and it would need the ROCm venv to gain it too, for no
  benefit over memory-mapped `.npy` here.
- Reading straight from SQLite each epoch: far too slow.

### D3. Prepared inputs on disk, block-shuffled streaming to the GPU

After the split and vocabulary are fixed, the training and validation rows' model inputs are
written once to disk as memory-mapped arrays:

| Array | Shape | Type |
|---|---|---|
| `cats` | (n, 8) | `int32` |
| `nums` | (n, n_num) | `float32` |
| `y` | (n,) | `float32` |

At about 90 M training rows that is about 9 GB.

Each epoch permutes fixed blocks of ~1 M rows. For each block it reads the rows into pinned host
memory, shuffles within the block, and copies batches to the GPU, casting `cats` to `int64` on
the device. Peak host memory is a few blocks plus the page cache, which the kernel may reclaim.
The job's RSS no longer scales with the dataset.

`ArrivalNet.fit`/`predict_arrays` take a batch source instead of whole arrays. A single guard
function checks every tensor the loop creates (input batch, target batch, activations via the
existing `safe_batch` rule) against 2^31 bytes before the first step.

Alternatives considered:
- Keeping everything in host RAM: it fits in 30 GB but not reliably under the 14 GiB cap with
  validation, model and prep alongside.
- A full random shuffle each epoch from a memmap: random 100-byte reads, much slower than block
  shuffling. Block shuffling with 1 M-row blocks over ~90 blocks mixes dates well enough for SGD.

### D4. Split rule: 7-day sealed test, 28-day validation, rest training

`split.py` gains `rolling_split(rows_per_date, inspected, test_days=7, val_days=28,
min_train_days=90)`. It drops stub dates, takes the latest `test_days` complete dates not listed
as inspected as the test range, the `val_days` complete dates before them as validation, and
everything earlier as training. The existing `training_history_before` rule (runs finalized
before the earliest validation cutoff) is applied unchanged.

Inspected dates are kept in `data/ml/inspected_dates.json`. A final test appends its dates, so
scoring them again requires newer data. The hard-coded lists in `probe_arrival.py` are no longer
used by the new scripts.

After selection, the chosen configuration is refit on training + validation dates, for the
number of epochs its validation run chose, before scoring on test. The deployed model then
includes the most recent four weeks. Fitting data still ends before every test cutoff.

Alternatives considered:
- `split_dates`' 67/13/20 shares: at a year that is ~70 test days, far more than needed, and a
  test range that old delays deployment evidence.

### D5. Median everywhere, MAE for early stopping and ranking

- `NetConfig.point_rule` defaults to `median`. `window` stays in code only to re-score the
  deployed artifact.
- Early stopping in `ArrivalNet.fit` ranks epochs by validation MAE (ties: within-3 share).
- `search` ranks trials by validation MAE, then coverage, then asset size.
- Reports add a per-calendar-month breakdown and the share of the single most frequent point
  value (the spec's near-constant check).

### D6. Train vocabulary pruning

`restrict_to_training_vocab` gains `min_count=20` and `recent_days=56`, measured against the
last training date. A train keeps a code only if both hold. Only the train column is pruned;
stations, lines and the rest keep today's rule. Both thresholds go into the manifest `prep`, and
prediction encodes through the saved vocabulary, so nothing changes at serving time.

56 days spans the December timetable change by about a month and a half after it happens, so
pre-change train numbers age out within two months of the change.

### D7. Candidates and size budgets: a compact track and a large track

There are two tracks over the same prepared inputs, split and vocabulary. Each track picks its own
winner by validation MAE.

**Compact track (deployable, ≤ 35 MB).** A fixed list of about 9 trials, all with median point
rules, checkpointed per trial as today:

- MLP, quantile head:
  - 512 × 3 blocks, train embedding 16
  - 512 × 3 blocks, train embedding 32
  - 1024 × 3 blocks, train embedding 32
- MLP, binned head, median rule: 512 × 3 blocks, train embedding 32
- TabM, quantile head:
  - k = 8, 256 × 2 blocks, train embedding 32
  - k = 16, 512 × 2 blocks, train embedding 32
- Histogram, median rule, `min_support=20` (baseline; last time's best MAE; its size is
  measured, not assumed)
- The best of the network trials above, without `doy_sin`/`doy_cos` (ablation)
- The two-sided TabM (incumbent) and the window MLP, re-scored on the same validation and test
  rows (references, not retrained)

**Large track (comparison-only, ≤ 100 MB).** About 4 trials, with the same heads, rules and
selection score. They differ only in the capacity the larger budget allows:

- MLP, quantile head:
  - 1024 × 4 blocks, train embedding 64
  - 2048 × 3 blocks, train embedding 64
- TabM, quantile head: k = 16, 1024 × 3 blocks, train embedding 64
- The large-track winner's shape with a looser train pruning (`min_count=10`), because the budget
  can afford more train entries

With ~100 k trains, a 64-wide train embedding is ~26 MB. A 1024 × 4 backbone is ~34 MB and
2048 × 3 is ~100 MB, so the pre-trial size estimate will likely skip the 2048-wide trial unless
the vocabulary is small. Both tracks use the same 2^31-byte guard. At 1024 wide, k = 16 and batch
8192, the largest activation is ~0.5 GiB. The large track has its own time cap of 10 h, run as a
second launch job after the compact track finishes. The GPU is shared, so the two tracks run one
after the other.

A size estimate is computed from the vocabulary before each trial trains:
4 bytes × (Σ card·dim + backbone parameters) + prep JSON. A trial whose estimate exceeds its
track's budget (35,000,000 or 100,000,000 bytes) is skipped with a logged reason. Export measures
the real ONNX size and refuses anything over the budget.

Rough numbers for the compact track, assuming pruning leaves ~100 k trains: train embedding 32 →
~13 MB, backbone 512 × 3 → ~6 MB, 1024 × 3 → ~25 MB. The 1024-wide trial may be skipped by the
estimate if the vocabulary is larger than expected.

The compact track has a time cap of 10 h. Per-trial time is extrapolated from the 2026-09-26
trials (54 s MLP, 150 s TabM at ~7 M rows) as roughly ~10–15 min per MLP and ~30–45 min per TabM.
Large-track trials are expected to take 2–4× as long per trial.

### D9. Comparison-only artifacts live outside the served directory

The large model is exported with the same `save_arrival_artifact` path, into
`data/models/v2-comparison/<stamp>/`, with `"role": "comparison"` in its manifest. Deployable
artifacts carry `"role": "deployable"`, and older manifests without a role count as deployable.

Two independent guards keep a comparison model off the website:
- The web server only reads `ML_MODELS_DIR` (`data/models/v2/`) through `CURRENT`, and that
  directory never contains a comparison artifact.
- `promote` refuses any artifact whose manifest role is `comparison`, even if it is copied into
  `v2/` by hand.

Export takes the budget from the role (35,000,000 bytes for deployable, 100,000,000 for
comparison) and refuses larger artifacts.

`dbrel ml predict-delay` gains `--artifact PATH`, which builds `DelayModel(PATH)` directly instead
of `DelayModel.current(...)`. The report scripts load the comparison artifact by path in the same
way.

Alternatives considered:
- The same directory with a role flag only: one guard instead of two, and a stray `promote` or a
  future serving change could expose a 100 MB download.
- Not exporting the large model at all (just scoring it at training time): rejected, because you
  want to query it from the CLI and re-score it in later comparisons.

### D8. Browser and export support for arrival-only quantile output

- `save_arrival_artifact` accepts `output: "quantiles"` with `edges: null`. `browser_meta`
  passes it through.
- `tabm.ts` gains a branch for `kind: "arrival-net", output: "quantiles"`, with output shape
  (rows, 3): p25/p50/p75, point = p50, rule `median`.
- The existing browser check (`scripts/browser_check`) is extended to this output and must match
  the Python predictor on the same inputs.
- `web/queries._model_input` passes the service date it already has into the new raw column.
  Views that use today's date for the model (board, line, train) keep doing so, which is correct
  for "now".

## Implementation notes (where the code differs from the decisions above)

Recorded 2026-09-29, when the pipeline code was finished and tested on synthetic fixtures only.

- **D1.** `doy_sin`/`doy_cos` are not added to `NUMERIC`. They form `SEASONAL`, and
  `ARRIVAL_NUMERIC = NUMERIC + SEASONAL` is what the full-year models use. `NUMERIC` is
  unchanged, so the deployed TabM and the window MLP keep encoding exactly as before. A prep
  whose numeric list differs from `NUMERIC` records it as `features_numeric`; a prep without it
  means `NUMERIC`. The seasonal columns are already in [-1, 1], so they are neither clipped nor
  rescaled (stats [0, 1]).
- **D2, storage.**
  - Each column is a raw little-endian `.bin`, not an `.npy` written through `open_memmap` after
    a `COUNT(*)`. The builder writes as it reads. The manifest records each shard's row count,
    and the loader refuses a column whose size doesn't match.
  - There is one root, `data/ml/arrival_full/`, not a directory per build stamp. A rebuilt month
    replaces its shard by a directory swap.
  - Shards store raw inputs; features are computed on read (`FullHistory.numeric`), so a feature
    change needs no rebuild.
  - Train directions are stored per build.
  - A month is also rebuilt when its fingerprint changes or the builder's `CODE_VERSION` changes,
    not only when its count changes.
- **D2/D3, reading.** Columns are read with plain file reads (`full_year.FileColumn`,
  `np.fromfile`), not `mmap_mode="r"`. Pages of a memory-mapped file count towards the job's RSS
  under the memory cap.
- **D3.** Means and standard deviations are exact, from streamed moments. The robust-scaling
  quantiles are estimated from a random sample of at most 2 M training rows
  (`training_statistics`); the prep records the sample share. Prepared sets also store
  `stop_id`, `day`, `segment`, `sched_min` and `is_holiday`, for the reports and the histogram
  baseline.
- **D5.** The old within-3 `OBJECTIVE` stays, so earlier experiment keys still match. The new
  searches use `MAE_OBJECTIVE`.
- **D7.**
  - The large track's networks set `embedding_max_dim` to 64. Otherwise every embedding is
    capped at 32 wide, whatever `train_emb_dim` says.
  - The size estimate counts what a browser would download: the model and the manifest
    (`download_bytes`). The export check uses the same figure.
  - The histogram has no size estimate before training; its size is measured after fitting.
  - The winner is the smallest full-coverage trial within 2% of the best validation MAE (see
    Risks).
  - The ablation and looser-pruning variants run only after all of a track's fixed trials have
    finished or been skipped.
  - 2026-10-01: both tracks list their TabM trials first, so a track that hits its time cap
    drops MLP trials rather than TabM ones. The ranking is unchanged (validation MAE).
  - 2026-10-02: early-stopping patience is 6 epochs instead of 3. In the dry run validation MAE
    moved by 0.1–0.2 min between epochs (2.98, 2.92, 2.90, 2.78, 2.92, 3.05, 2.79) and training
    stopped at epoch 7, before the 12-epoch OneCycle schedule reached its low-learning-rate phase.
    The compact trial that exceeds the 35 MB budget is the MLP 1024×3 (about 39 KB over, 35,039,325
    estimated bytes before the notice rates, 35,089,654 after); it stays skipped by decision, not
    resized. The 512×2 TabM (10.8 MB) is within budget and trains.
- **D4 / task 5.5.**
  - `final` refits and scores every requested track in one run, so all models are scored on
    identical test rows.
  - Experiment directories are named `<track>-<last test date>`, so a later run on newer data
    starts a new experiment instead of clashing with the old experiment's saved key.
- **D8.** The ONNX export moved from `scripts/export_arrival.py` into `arrival_nn.export_onnx`.
  `search_full_year.py export` also writes the `scripts/browser_check.py` layout next to each
  final model, with its test-week reference rows, so the unchanged browser page checks it.
- **D9.** `dbrel study live-accuracy --artifact PATH` scores any artifact. The report
  names the artifact and flags a comparison model.
- **Memory check (task 4.4).** The full-year probe is `search_full_year.py memory-check --track`,
  not `dbrel ml memory-check`, which stays bake-off specific. Each probe runs in its own process,
  because a process's peak RSS never falls. The result is `memory_check-<track>.json`, with one
  projection for the search (training rows) and one for the final refit (training + validation
  rows). `dbrel ml launch` refuses a `search`/`final` step if that file is missing, was made on a
  different full-history build, or projects above the cap.

## Risks / Trade-offs

- **Time of year is confounded with one-off events of 2025/26.** With each calendar day seen once,
  a closure or storm can be learnt as a seasonal effect and repeated next year.
  → One harmonic only (D1), a per-month report, and the ablation trial showing whether the
  feature helps at all on validation.
- **Seasonality can't be truly validated yet.** Validation and test are the latest weeks, whose
  time-of-year neighbours in training are only the weeks just before them. October has never been
  seen. → Stated in the report's disclosures. Re-evaluate once the data spans more than a year.
- **The backfill may be incomplete or uneven across months.** → The build manifest's rows-per-date
  table and the stub-date rule show it. The report lists training days per month.
- **Streaming is slower than all-in-GPU training.** → Pinned memory and 1 M-row blocks. If an
  epoch over ~90 M rows exceeds ~10 min, the trial list is cut before the time cap rather than the
  data being subsampled silently.
- **The vocabulary is larger than estimated.** → The pre-trial size estimate skips oversized
  configurations, and `min_count`/`recent_days` are recorded and adjustable without code change.
- **A median model may look worse on "within 3 minutes" than the deployed window model.** That is
  expected; the report shows both scores side by side so the trade-off is explicit at approval.
- **The large model wins clearly but can't be deployed.** → That is by design. The report shows
  the gap, which tells us whether a later change should revisit the browser budget or distil the
  large model into the compact one.
- **A 35 MB download on phones.** → Prefer the smallest artifact within ~2% of the best
  validation MAE when choosing. The report lists size next to score.

## Migration Plan

0. Done 2026-09-29: `CURRENT` moved back to the two-sided TabM (`20260924T183944Z`). The window
   MLP (`20260926T113337Z`) stays on disk.
1. Build the full-history dataset once the backfill covers the intended window, and check its
   per-month row counts.
2. Run the compact track, then the large track, as capped `dbrel ml launch` jobs. Refit each
   track's winner on training + validation and score both once on the sealed test week, together
   with the TabM and the window MLP.
3. Export the compact winner to `data/models/v2/` (`CURRENT` unchanged), and run the browser
   parity check and size check. Export the large winner to `data/models/v2-comparison/`.
4. Present the report. On approval, `promote` moves `CURRENT` to the compact model.
5. Rollback: `promote` back to `20260924T183944Z` (or `20260926T113337Z`). Both stay on disk, and
   the frontend keeps supporting their output kinds.

## Open Questions

- Whether to include the backfilled days before the first live-collected day (2026-08-21)
  differently if archive data turns out to be noisier than live polling. This can be judged from
  the per-month report without changing the design.
