# Design

## Context

See proposal.md (Why). The current state that shapes this design:

- **Data.** `data/dbrel.sqlite` (5.9 GB) holds 14.3M `trip_stop` rows over service dates
  2026-08-24 … 2026-09-22 (30 full days, ~400–520k stops/day; 08-21/08-23 are stubs).
  - 12.1M rows are `RECORDED`; 11.2M have a departure delay and 11.2M an arrival delay.
  - The rest: 1.26M `PREDICTION_ONLY`, 0.71M `NO_DATA`, 0.23M `CANCELLED`.
  - 5,422 tracked stations, 73,375 trains (2,605 have no line), 853 lines.
- **Target shape.** From a 1/50 sample of departure delays within ±2 h:
  - mean 3.2 min; p10 0, p50 1, p90 8, p99 24 min
  - 33% of stops are ≤0 (on time or early), 84% are under 6 min
  - This is zero-inflated and heavy-tailed.
- **Existing building blocks to reuse**, not re-derive:
  - `derive/stop_averages.py`: the `RECORDED` / `cancelled = 0` / `MAX_PLAUSIBLE_DELAY_S = 7200`
    filters and the per-trip start-time SQL (`_TRIP_START_TTS_SQL`, from `source_run_key`).
  - `direction.py`: `direction_by_train_id` for lines and `corridor_trains_by_direction` for
    line-less trains.
  - `trip_stop.is_holiday` and the holiday calendar in `holidays_de.py`.
  - `web/queries.py`: `_train_trip_start_time` and `_sched_time_key` define the inference-side
    keys.
- **History.** The old pipeline (`src/dbrel/ml/`, deleted in e725f18) got some things right that
  are kept: date-based splits, a single row builder shared by training and scoring, and a
  `-1`/unknown category code. Its choices were tuned on a 212-station pilot. They are
  deliberately not inherited: no fixed `num_leaves=63`, and early stopping is added.
- **Environment.** Ryzen 7 7700 (8 cores / 16 threads), 30 GB RAM, AMD Radeon RX 9070 XT
  (RDNA4, gfx1201, 16 GB VRAM; no CUDA). ROCm 7.2.4 core + rocminfo are installed and detect
  the card; the HIP runtime/SDK and ROCm OpenCL are not yet installed. CachyOS (Arch-based). lightgbm 4.7.0 and numpy 2.5.3
  are already installed in `.venv`.

### Research summary

Background research on published work and libraries informs the decisions below:

- GBDTs (LightGBM/XGBoost) and random forests dominate train-delay prediction on tabular data.
  Deep models appear mainly for live network-propagation tasks.
- Schedule-only setups (no live state) report MAE of roughly 1.7–3.7 min (NL, FI studies). With
  live upstream delays the reported MAE falls to 0.5–2 min. That is out of reach here by design.
  Expect the 2–4 min range.
- CatBoost often wins untuned accuracy on generic benchmarks, but trains slowest on CPU.
  LightGBM is fastest at 10M+ rows and handles high-cardinality categoricals natively.
- XGBoost 2.x supports native categoricals, and multi-quantile training via
  `reg:quantileerror` with a list of `quantile_alpha` values.
- CatBoost `MultiQuantile` fits all quantiles jointly in one model. LightGBM needs one model per
  quantile.
- LightGBM's separately trained quantile models can cross (open issues #3447, #5727).
- Rejected alternatives:
  - TabPFN v2 is capped around 10k rows.
  - TabM and embedding MLPs need GPU-class compute.
  - NGBoost is too slow per round at 11M rows.
  - Quantile regression forests are less accurate than boosting at this size.
  - LightGBMLSS (distributional GBDT) is a credible follow-up if the plain quantile bands prove
    miscalibrated. It is left out of this change to keep the bake-off to the three standard
    libraries.

## Goals / Non-Goals

**Goals:**
- A reproducible pipeline in three commands: build dataset → bake-off → predict.
- Candidates are directly comparable: same rows, same split, same metrics, and each library gets
  the same tuning budget.
- An honest benchmark against what the app does today (averages), built without test-day leakage.

**Non-Goals:**
- Web UI or API integration. `dep_pred`/`arr_pred` stay backed by averages.
- Live or real-time features (upstream delay, current position, disruption messages).
- Cancellation probability.
- Backfilling more months of history.
- GPU training for LightGBM and CatBoost (see D10: CatBoost has no AMD support; LightGBM's
  GPU builds crash on categories with more than 256 values, and station/train/line all exceed that).
- Conformal calibration of the bands. The report measures band coverage, and calibration can
  follow if coverage misses.

## Decisions

### D1. Features (chosen by Anas, 2026-09-24)

| Group | Features | Source (training / prediction) |
|---|---|---|
| Identity | `station_id`, `train_id`, `line_id` (unknown→`-1`), `category`, `operator` — all categorical | same columns both sides |
| Calendar | `weekday` (cat), `is_holiday` (0/1) | training: `trip_stop.is_holiday`; prediction: the same holiday function applied to date + station state |
| Scheduled time | `sched_min` = minutes after local midnight of COALESCE(departure, arrival), mod 24 h | training: `supposed_*_utc` → Berlin time; prediction: `station_stop.planned_*_sec` |
| Start time | `trip_start_min` | training: per-trip `source_run_key` stamp (same SQL as averages); prediction: `train.first_departure_sec` (as `_train_trip_start_time`) |
| Location on route | `minutes_since_start` (sched − start, wrapped), `stop_frac` (rank of the stop within its route variant ÷ variant length), `n_stops`, `is_origin`, `is_terminus` | ranked `station_stop` rows of the variant (`path_sha1`) both sides |
| Station & geography | `lat`, `lon`, `state` (cat) | `station` |
| Direction | `direction` (0/1; `-1` when unresolved) | line trains: `direction_by_train_id`; line-less: `corridor_trains_by_direction` relative to the corridor anchor |

Interpretation note: Anas's answer listed "location" and "geography" separately. "Location" is
the stop's position along its route. "Geography" is coordinates and state. Anas confirmed this
reading on 2026-09-24.

Explicitly excluded:
- `service_date`, day-of-year and any date trend. With 30 days these would memorise the period.
- Platform. It is a live-changeable field, and the congestion group was not chosen.
- Everything from `tt_message`, `changed_platform`, actual times and other runs.

**Stop rank, not `stop_sequence`.** `stop_sequence` is the sparse IRIS stopIndex and shifts
between route variants (memory: 9% of trips misattributed before path_sha1). The rank within
`(train_id, path_sha1)` is stable. If a trip's variant has no `station_stop` rows, the location
features that need rank are `NaN`, which all three libraries accept as missing. The row is not
dropped.

**One row builder.** A single function builds a feature row, as in the old pipeline, and the
prediction path calls it too. A test builds the row for a historical stop via both paths and
asserts equality. This covers the spec scenario "Training and prediction build features the same
way".

### D2. Targets and filters
- Two targets per row: `arr_delay_s` and `dep_delay_s`, each in minutes (float). Each side gets
  its own models.
- Filters, copied from the averages builder so the baseline and candidates see the same
  population:
  - `quality = 'RECORDED'`, `cancelled = 0`
  - per side: `delay IS NOT NULL AND ABS(delay) <= 7200`
  - `is_holiday IS NOT NULL`
- No clipping below 2 h. Quantile loss is already robust to the tail.

### D3. Dataset materialisation
`dbrel ml build-dataset` does the following:
- Streams one SQL join, `trip_stop ⋈ trip ⋈ train ⋈ station ⋈ station_stop` (rank via a
  window function).
- Resolves directions once, into a train_id→direction dict.
- Writes `data/ml/dataset.npz` (column arrays) plus `dataset.json`. The JSON holds the
  vocabularies, the date range, the row counts and a fingerprint of the database's contents
  (row counts and high-water marks of trip_stop, trip, station_stop and train).

The bake-off refuses a dataset whose fingerprint no longer matches, unless `--force` is given.
The fingerprint reads the data, not the file. In WAL mode a commit can leave the main file's
mtime and size unchanged, and a checkpoint can change them without any data changing (found
while implementing). Expected size: ~11M rows × ~20 columns ≈ 1–1.5 GB in RAM. Expected build time: 15–30 min.

Alternative considered: querying SQLite inside each training run. Rejected, because the bake-off
reads the data dozens of times.

### D4. Split (by whole service date, chronological)
- **train**: 2026-08-24 … 2026-09-12, 20 days (3 weekends)
- **validation**: 09-13 … 09-16, 4 days (incl. Sunday 09-13)
- **test**: 09-17 … 09-22, 6 days (incl. weekend 09-19/20)

Stub days before 08-24 are dropped. Boundaries are computed from the dataset's dates as
contiguous 67/13/20 % ranges, so a later rebuild with more days shifts them automatically.
`dataset.json` records the actual dates.

### D5. Quantile setup per library
- **LightGBM**: `objective="quantile"`, one booster per α ∈ {0.25, 0.5, 0.75} per side. It uses
  native categoricals. Hyperparameters are tuned on α = 0.5. p25 and p75 reuse those parameters,
  each with its own early stopping.
- **CatBoost**: `loss_function="MultiQuantile:alpha=0.25,0.5,0.75"`, one model per side, using
  native categoricals with ordered target statistics.
- **XGBoost**: `objective="reg:quantileerror"`, `quantile_alpha=[0.25,0.5,0.75]`,
  `tree_method="hist"`, `enable_categorical=True`, one model per side, `device="cuda"` on the
  ROCm/HIP build (D10). If the GPU build or its smoke test fails, it falls back to
  `device="cpu"` and the report says so.
- **Ordering fix** (all libraries): sort the three outputs per row. This satisfies the "ordered
  percentiles" requirement at negligible cost. The report also logs each candidate's raw crossing
  rate.

### D6. Tuning (Optuna)
- **Sampler**: TPE with a median pruner.
- **Objective**: mean validation pinball loss over the three quantiles, computed on the departure
  side. The departure side has more samples and is what search results show. The winning
  parameters are then reused for the arrival side.
- **Budget**: `--time-budget` of 60 min per library by default, up to `--trials` 50.
- **Subsampling**: trials train on a 3M-row random sample of the training days and evaluate on
  the full validation days. Early stopping uses a 50-round patience.
- **Final fit**: refit on all training days with the best parameters and the early-stopped round
  count. Validation days stay held out, so the winner is picked on them.

Search spaces:

| LightGBM | CatBoost | XGBoost |
|---|---|---|
| `num_leaves` 31–1023 (log) | `depth` 4–10 | `max_depth` 4–12 / `max_leaves` 0–1023 |
| `learning_rate` 0.02–0.2 (log) | `learning_rate` 0.02–0.2 (log) | `eta` 0.02–0.2 (log) |
| `min_data_in_leaf` 20–2000 (log) | `l2_leaf_reg` 1–30 (log) | `min_child_weight` 1–200 (log) |
| `feature_fraction` 0.5–1.0 | `rsm` 0.5–1.0 | `colsample_bytree` 0.5–1.0 |
| `bagging_fraction` 0.5–1.0 (freq 1) | `bootstrap_type` Bernoulli, `subsample` 0.5–1.0 | `subsample` 0.5–1.0 |
| `lambda_l2` 0–50 | `one_hot_max_size` 2–64 | `lambda` 0–50 |
| `cat_smooth` 1–100, `max_cat_threshold` 16–256 | `border_count` 128–254 | `max_cat_to_onehot` 1–64, `max_bin` 128–512 |
| up to 5000 rounds + early stop | up to 5000 iters + early stop | up to 5000 rounds + early stop |

Anas asked to be involved in hyperparameter decisions. He reviewed and approved these ranges
unchanged on 2026-09-24. They are editable constants in one module, not buried in code.

### D7. Baselines and scoring
- **Averages baseline**:
  - Group the *training-day* rows in memory by the stored averages' key:
    - line trains: (station, line, direction, trip start, sched time, holiday)
    - line-less trains: (train, station, trip start, sched time, holiday)
  - Take the empirical p25/p50/p75 of each group, so the baseline has the same band shape.
  - The stored tables are never read. They were built from all 30 days, including the test
    days.
  - Keys without training rows are counted as "not covered".
- **Global-median baseline**: the overall training p25/p50/p75 per side. It covers every row.
- **Metrics** on test days, per side:
  - MAE of p50, in minutes
  - pinball loss at each α
  - p25–p75 coverage (target: ~50%)
  - share of rows covered
  - Breakdowns by category (FV / RV / S) and holiday flag
- **Winner**: chosen per side by lowest mean validation pinball among the three candidates.

### D8. Artifacts and CLI
Artifacts are written to `data/models/v2/<UTC timestamp>/`:
- `arr.*` and `dep.*`: the native model files (`.txt` / `.cbm` / `.ubj`)
- `manifest.json`: features, vocabularies, split dates, the library per side, library versions,
  best parameters, and all candidate and baseline metrics
- `report.md`: the comparison table

The directory is written under a temporary name and renamed when complete. Then
`data/models/v2/CURRENT` (a one-line text file) is updated atomically. Losing candidates' models
are not kept; their scores are. `--keep-candidates` keeps them.

The commands are a `ml` Typer sub-app:
- `dbrel ml build-dataset`
- `dbrel ml bakeoff [--libraries lightgbm,catboost,xgboost] [--time-budget 60] [--trials 50]`
- `dbrel ml report [ARTIFACT]`, which reprints the table
- `dbrel ml predict-delay --train "ICE 599" | --line RE22 --station "Köln Hbf" --date
  2026-09-25 [--time 08:00] [--direction …]`
  - With `--train`, it predicts that train's call at the station.
  - With `--line`, it predicts the line's scheduled calls at that station nearest `--time`, in
    the given direction. This mirrors the search view's five inputs.
  - Output: scheduled time, then `p50 (p25–p75)` in minutes for arrival and departure.

The `ml` sub-app imports `dbrel.ml` lazily, so `dbrel --help` works without the extra.

### D9. Dependencies
The `ml` extra becomes `lightgbm>=4.7, numpy>=2.0, catboost>=1.2, xgboost>=2.1, optuna>=4`.
The `xgboost` requirement is satisfied by the locally built ROCm wheel (D10). On a machine
without it, pip's CPU wheel is used instead.
Nothing outside `src/dbrel/ml/` may import them. The pyproject comment about why is kept.

### D10. Devices: XGBoost on the AMD GPU, LightGBM and CatBoost on the CPU (Anas, 2026-09-24)

Research findings (web-sourced; points marked unverified were not confirmed for this card):
- **CatBoost**: CUDA only. No AMD support. It stays on the CPU.
- **LightGBM**: stays on the CPU. It has two GPU options, and both fail here:
  - The ROCm build (upstream `USE_ROCM`, and AMD's 4.6.0.99 build) crashes on categorical
    features with more than 256 values.
  - The OpenCL build has known failures with high-cardinality categoricals. There is an open
    bug report that ROCm's OpenCL wedges the gfx1201 until reboot.
- **XGBoost**: the most viable GPU route. AMD maintains a ROCm build
  (`-DUSE_HIP=ON -DCMAKE_HIP_ARCHITECTURES=gfx1201`). Caveats:
  - gfx1201 is not one of its documented targets (unverified).
  - The prebuilt ROCm wheels target Python 3.12, and this venv is 3.13, so it is built from
    source.
  - Whether `reg:quantileerror` and `enable_categorical` work on this card is unverified. The
    code path is shared with CUDA.

Setup, in order, each step verified before the next:
1. Install the HIP runtime and SDK from the official repos with
   `sudo pacman -S rocm-hip-sdk rocm-hip-runtime` (7.2.4, matching the installed `rocm-core`).
   Anas runs this himself, since it needs sudo.
2. HIP smoke test. Run `hipInfo` or a trivial HIP program to confirm gfx1201 is accepted. There
   is an open report of gfx1201 being rejected because it reports two ISAs.
   `HSA_OVERRIDE_GFX_VERSION` should not be needed, because ROCm 7.2 lists the card as
   supported.
3. Build XGBoost from source against HIP for gfx1201. Install the resulting wheel into `.venv`.
   Keep the build script in `scripts/build_xgboost_rocm.sh` so it is reproducible.
4. Correctness check. On a sample of ~1M dataset rows, fit the same quantile model on
   `device="cuda"` and on `device="cpu"`. Validation pinball losses must agree within 1%, and
   the GPU fit must not error on the categorical columns. Record the timings of both fits.

The fallback is automatic and never blocks the bake-off. If any step fails, XGBoost runs on
the CPU and the report records "GPU: unavailable (<reason>)". Steps are not retried silently:
each failure is surfaced to Anas, with its scope, before any more time goes into it.

**Outcome (2026-09-24): the GPU route failed the correctness check, and XGBoost runs on the
CPU (Anas's choice).**
- Upstream XGBoost 3.4.1 has no HIP build option. AMD's fork (`ROCm/xgboost`, `release/3.2.0`)
  does, and its HIP builds also need `rccl`. It built and installed for gfx1201. It trains on
  the GPU about 14x faster than 2 CPU threads, but its results are wrong:
  - Same parameters, same 1M rows: GPU mean pinball was 7.2% worse than CPU.
  - A squared-error model on the GPU reached an RMSE of ~10^16 min, against 6.5 min on the CPU,
    with and without categorical columns.
- The quantile and absolute-error objectives only hide the fault partly: they recompute each
  tree's output values after building it. The most likely cause is that the fork does not
  support RDNA4, which is not one of its documented targets.
- The fork's CPU path matches earlier CPU runs, so the HIP build stays installed and XGBoost
  simply runs with `device="cpu"`.
- LightGBM ran first, on 14 threads, while the build compiled on 2 jobs. CatBoost and XGBoost
  then ran one after the other on all 16 threads, reusing LightGBM's cached result.

**LightGBM on the GPU, tried too (2026-09-24, Anas's request; experiment only).**
- AMD's prebuilt `amd_lightgbm` 4.6.0.99 wheel carries gfx906 code only. A source build of
  `AMD-Ecosystem/LightGBM` `release/4.6.0.99` for gfx1201 works
  (`scripts/build_lightgbm_rocm.sh`, into its own venv under `data/build/`).
- It matches the CPU on purely numeric data (1M real rows, 100 rounds, 18.5 s).
- It fails as soon as any feature is categorical:
  - only the small ones (state, weekday, direction): `[CUDA] invalid argument` in
    `cuda_single_gpu_tree_learner.cu`
  - all categoricals: a GPU page fault that kills the process. The card recovers.
- The model depends on its station/train/line categoricals, so LightGBM stays on the CPU.
- A numeric-only variant was also tried (`scripts/experiment_lightgbm_gpu_numeric.py`: each
  categorical as its code plus its training-day frequency). Squared error on 1M real rows,
  200 rounds: GPU 6.4828 in 44.1 s, CPU 6.4801 in 15.2 s on only 2 threads. So it is correct,
  but 3x slower than 2 CPU threads. Anas stopped it before the full fits.
  `scripts/check_lightgbm_rocm.py` is the check to rerun against a future fork release.

**Fairness.** Every library gets the same wall-clock tuning budget, so GPU XGBoost may finish
more Optuna trials than the CPU libraries. That is intended: the bake-off compares what each
library achieves in the same time on this machine. The report lists each library's device and
trial count. It also lists the CPU-vs-GPU timings from step 4, so any hardware advantage is
visible.

**Concurrency: on by default (Anas, 2026-09-24).** XGBoost on the GPU needs little CPU, so it
is tuned and fitted at the same time as the CPU libraries. That saves ~1 h of wall-clock time.
- **Thread split.** XGBoost gets 2 threads, to feed the GPU. The CPU library running alongside it
  gets the remaining 14. That keeps the CPU budgets honest: the 14 threads are the same whether
  or not XGBoost is running.
- **Memory.** Only XGBoost's own copy of the training data sits in VRAM. The host RAM needed for
  XGBoost plus one CPU library stays within 30 GB. The risk on memory below still applies to the
  CPU libraries, which run one after the other.
- **Fallback.** If XGBoost fell back to the CPU (D10), everything runs sequentially with all 16
  threads each, since two CPU libraries at once would distort their budgets.
- **Opt-out.** `--no-parallel-gpu` forces the sequential order.

## Risks / Trade-offs

- **Only 30 days → no seasonality.** The model cannot learn winter, leaf-fall or timetable-change
  effects. → Stated in the report. More history is a separate later change (Anas: train on 30
  days first).
- **Test period is just 6 days.** A single disruption (storm, strike) can dominate the test
  scores. → Report per-day MAE next to the totals, so an outlier day is visible.
- **High-cardinality `train_id` (73k) can overfit or bloat models.** → `min_data_in_leaf` /
  `cat_smooth` / `max_cat_threshold` are in the search space. The report gives model size, and an
  ablation run without `train_id` is a cheap follow-up if models exceed ~100 MB.
- **CatBoost on CPU may not fit its budget.** At 3M-row trials it could manage only a few trials
  in 60 min. → The time budget is per library and reported. A library with fewer than 5 finished
  trials is flagged "under-tuned" in the report, rather than silently compared as equal.
- **Averages baseline favoured on well-sampled keys, model favoured on sparse keys.** → The report
  also gives MAE on the subset the averages cover, so the comparison is like-for-like.
- **Prediction-side start time differs slightly from training.** Training uses the per-trip
  stamp; prediction uses per-train `first_departure_sec`. They are equal except for train numbers
  that run at different times on different days. → Same compromise the web averages lookup makes
  today. The row-equality test runs on a train where they agree.
- **The XGBoost ROCm build may fail, or misbehave, on gfx1201.** The target is undocumented, and
  there are open ROCm issues for this card. → The D10 correctness check compares GPU and CPU
  results before the GPU is trusted, and the automatic CPU fallback keeps the bake-off
  unblocked.
- **GPU errors could leave the card unusable until reboot.** One reported bug (ROCm OpenCL on
  gfx1201) does this. → Only the HIP path is used, never ROCm OpenCL. If the desktop shows GPU
  errors after a run, the GPU route is dropped.
- **Memory.** Three libraries' training matrices at once would exceed RAM. → The CPU libraries run
  sequentially (GPU XGBoost runs alongside them, per D10), and each frees its matrices before the next starts.

## Migration Plan

This change is additive. There is no schema change, and the database is only read.
- **Rollback**: delete `data/models/v2/` and `src/dbrel/ml/`, and drop the `ml` sub-app
  registration.
- The web app is untouched.
- The stale `data/models/v1/` artifacts from the deleted pipeline are left in place. Removing them
  is Anas's call.

## Open Questions

- **The model is meant to run in the browser (Anas, 2026-09-24).** That rules out the
  LightGBM candidate as the deliverable: its six quantile models total **3.0 GB** of text.
  Categorical splits on the 61k-value train number and the 5k-value station store large
  value sets, over thousands of trees. It stays in the bake-off as the accuracy benchmark.
  - The GPU-trained TabM (see the experiments in `scripts/experiment_nn.py` and
    `scripts/tune_tabm.py`) is ~20 MB in float32, 77% of it the train-number embedding.
    It is the browser candidate.
  - Its search records each trial's float16 size, rejects anything over 35 MB, and reports
    the best model under 2/5/10/20/35 MB.
  - Browser inference (ONNX export, delivering feature inputs by API or as static files,
    frontend wiring) will be its **own OpenSpec change**, proposed after this change's report.

- Whether a winning model later replaces the web UI's averages is decided after Anas sees the
  report. That will be a separate change (it would modify `web-frontend` and
  `stop-delay-averages`).
