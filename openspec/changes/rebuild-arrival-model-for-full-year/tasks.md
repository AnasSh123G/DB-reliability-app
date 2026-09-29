# Tasks

Before every task, tell the user its current duration estimate and the basis for it. Keep
hands-on work separate from unattended computation, and revise the estimates once measurements
change them. The ranges below are provisional.

Groups 1–4 need only the existing one-month data and can be done while the backfill runs.
Group 5 needs the backfill to cover the intended window. Nothing moves `CURRENT` without the
user's approval (task 6.6).

Already done outside these tasks, on 2026-09-29: `CURRENT` was promoted back to the two-sided TabM
`20260924T183944Z`. `tests/test_ml_arrival_deploy.py` passed, and the board API served that stamp
with model inputs for every call.

**Status, 2026-09-29: the pipeline code is complete and tested on synthetic fixtures only.** On
Anas's instruction, nothing has been built from or trained on the real data yet. The open items
below are all runs on real data, GPU runs, or approvals:

- 2.3: code and the one-shard test done. Open: the capped build of the real database and its
  recorded peak.
- 2.4: command, README section and fixture count test done. Open: the check on the live database.
- 4.2: block source, pinned blocks, device `int64` cast, 2^31-byte guard and the CPU tests done.
  Open: the ±0.02 min GPU parity run.
- 4.4: probe, projection, `memory-check` subcommand and the launch gate done (fixture: projection
  written, a low cap refuses). Open: the probe on the real data.
- 5.2: `scripts/search_full_year.py` done. Both tracks, the final refit and both exports ran end
  to end on the fixture (`tests/test_ml_full_year_search.py`). Open: the `--max-trials 1` dry run
  on the real data.
- 6.4: the browser check of the fixture's compact and large exports passed in headless Chromium
  (max difference about 1e-7 minutes, no differing points). Bun is not installed on this machine,
  so the frontend was built with `npx vite build` and typechecked with `tsc --noEmit` instead.
- 6.5: code, skill doc and tests done. Open: a study run on the real compact artifact (needs 6.2).
- 5.1, 5.3–5.5, 6.2 and 6.6 have not been started.

## 1. Time-of-year feature and median defaults

- [x] 1.1 Add `day_of_year_idx` to `RAW_COLUMNS` and `doy_sin`/`doy_cos` to `NUMERIC` in `ml/features.py`, computed from month/day on a 366-day index (design D1). Verify with `tests/test_ml_features.py`, which must cover:
  - the same month/day giving identical values in 2025 and 2026
  - 31 Dec → 1 Jan being one step, like any consecutive pair
  - the 28 Feb → 1 Mar non-leap-year skip
  - no year dependence

  Est.: 30–60 min.
- [x] 1.2 Fill the new raw column in the dataset query (from `trip.service_date`) and in `ScheduledCall.raw` (`ml/timetable.py`), which also covers `web/queries._model_input`. Verify with the existing "training and prediction build features the same way" test, extended so that a historical stop and the same scheduled call produce identical `doy_*` values. Est.: 30–45 min.
- [x] 1.3 Make `median` the default point rule in `NetConfig`. Switch early stopping in `ArrivalNet.fit` to validation MAE (ties: within-3), and make trial ranking MAE → coverage → asset size. Keep `window` only for re-scoring old artifacts. Verify with `tests/test_ml_arrival_nn.py`: a fixture where MAE and within-3 disagree must pick the lower-MAE epoch, and a trial with missing predictions must not outrank a full-coverage one. Est.: 30–60 min.
- [x] 1.4 Add a per-calendar-month breakdown and the "share of the most frequent point value" figure to `metrics.score_arrivals_with_breakdowns` and the report. Verify with a synthetic two-month report whose month rows reproduce the per-instance records, and whose near-constant predictor shows a share ≥ 0.75. Est.: 30–45 min.

## 2. Full-history dataset builder

- [x] 2.1 Implement the monthly-shard builder (design D2):
  - `COUNT(*)` per month, then columns written via `open_memmap`
  - text columns stored as `int16` codes from a saved dictionary
  - raw `int32` ids
  - `day` as days since epoch
  - file-backed temp store for `ml_stop_rank`
  - a build manifest with per-shard counts, rows per date and the DB fingerprint

  Output goes to `data/ml/arrival_full/<stamp>/`, never over `arrival_dataset.npz`. Verify with a test on a small SQLite fixture spanning two months: the shard contents must equal what the current `_build_dataset(arrival_only=True)` produces for the same rows (after code mapping). Est.: 2–3 h.
- [x] 2.2 Add incremental rebuild: only the latest month and months whose count or fingerprint changed are rebuilt. Verify with a test that adds rows to one month of the fixture and checks that only that shard's files change. Est.: 45–90 min.
- [ ] 2.3 Add a memory-mapped loader returning the same `Dataset` fields for chosen date ranges, without reading unselected rows. Verify with a test that loading one month of a two-month fixture touches only that shard. Also run a build of the current one-month database under `dbrel ml launch --memory 14G` and record its peak memory (`memory.format_memory`) in the change notes. Est.: 1–2 h plus ~10 min unattended.
- [ ] 2.4 Add a `dbrel ml build-full-dataset` CLI command with progress per month, and document it in the delay-model section of `README.md`. Verify that running it on the live DB prints per-month row counts matching `SELECT COUNT(*)` with the same filters. Est.: 30–45 min.

## 3. Splits and vocabulary

- [x] 3.1 Implement `rolling_split` (test 7 / validation 28 / min training 90 complete dates, stub dates excluded) and `data/ml/inspected_dates.json` handling (design D4). Verify with `tests/test_ml_split.py`, which must cover:
  - the windows moving forward when dates are appended
  - inspected dates never becoming test dates
  - the "too little data" error naming available vs required dates
  - runs never straddling sets

  Est.: 45–90 min.
- [x] 3.2 Add train pruning (`min_count=20`, `recent_days=56`) to `restrict_to_training_vocab`, recording both thresholds in `prep` (design D6). Verify with tests: a retired train and a rare train both encode as `UNKNOWN` in training and in `arrival_inputs` at prediction, and the manifest carries both thresholds. Est.: 45–60 min.

## 4. Streaming training and size guard

- [x] 4.1 Write the training/validation model inputs (`cats` int32, `nums` float32, `y` float32) to memory-mapped files once per split and vocabulary, keyed like the existing experiment key. Verify that re-running with the same key reuses the files, and that a changed vocabulary refuses to reuse them. Est.: 45–90 min.
- [ ] 4.2 Replace the whole-array GPU copies in `ArrivalNet.fit`/`predict_arrays` with a block-shuffled batch source (1 M-row blocks, pinned memory, `int64` cast on the device). Add one guard that checks every tensor the loop creates against 2^31 bytes before the first step (design D3). Verify:
  - a CPU test shows block iteration visits every row exactly once per epoch
  - a guard test refuses a configuration whose batch would reach 2 GiB
  - on the GPU, a one-month run's validation MAE is within ±0.02 min of the non-streaming result for the same seed and configuration

  Est.: 2–3 h plus ~15 min unattended.
- [x] 4.3 Add the pre-trial size estimate (embedding cards × dims + backbone parameters + prep JSON). Skip any trial estimated over its track's budget (35,000,000 bytes compact, 100,000,000 bytes large) with a logged reason (design D7). Verify that the estimate for the three existing ONNX artifacts is within 5% of their measured sizes, and that a test skips a 100 MB-track trial estimated at 101 MB. Est.: 30–45 min.
- [ ] 4.4 Extend `memcheck`/`launch` so the memory probe covers the streaming search at a fraction of full size and projects the peak at full size. Verify that the probe on the one-month data prints a projection, and that a deliberately low cap refuses to launch. Est.: 45–60 min.

## 5. Full-year search (needs the backfill)

- [ ] 5.1 Build the full-history dataset from the backfilled database. Check that rows per date and per month are plausible, and list months with stub or missing days. Verify that the build manifest exists, its total matches the SQL count, and its logged peak is under the cap. Est.: 15 min hands-on plus ~1–2 h unattended (extrapolated from the 246 s one-month build).
- [ ] 5.2 Write `scripts/search_full_year.py` with a `--track compact|large` option, and the fixed trial lists from design D7:
  - compact: the no-time-of-year ablation, the histogram baseline, and both old artifacts as references
  - large: the 100 MB trials, including the looser-pruning variant

  Each track gets its own experiment directory, a 10 h cap and per-trial checkpoints. Verify with a dry run on the one-month data (`--max-trials 1` per track) that both tracks write separate `ranking.json` files, and that the old TabM appears as a reference in both. Est.: 1–2 h.
- [ ] 5.3 Run the compact track as `dbrel ml launch full-year-compact --memory 14G`. Verify that `ranking.json` lists every trial or its skip reason, ranked by validation MAE, and the log shows the memory peak under the cap. Est.: 15 min hands-on plus ~6–10 h unattended.
- [ ] 5.4 After 5.3 finishes, run the large track as `dbrel ml launch full-year-large --memory 14G`. Verify as in 5.3, and that every trained large trial's size estimate is ≤ 100,000,000 bytes. Est.: 15 min hands-on plus ~8–10 h unattended.
- [ ] 5.5 Refit each track's winner on training + validation for its chosen epoch count. Score both once on the sealed test week, together with the two-sided TabM and the window MLP, then append the test dates to `inspected_dates.json`. Verify with `final.json`:
  - identical test rows for all four models
  - per-month and per-segment breakdowns and the most-frequent-value share for each
  - asset sizes
  - the disclosure that time of year is validated only on adjacent weeks

  Est.: 30 min hands-on plus ~1–2 h unattended.

## 6. Export, browser and deployment

- [x] 6.1 Add artifact roles: `save_arrival_artifact` takes a `role` of `deployable` (to `data/models/v2/`, budget 35,000,000 bytes) or `comparison` (to `data/models/v2-comparison/`, budget 100,000,000 bytes), and accepts `output: "quantiles"` with `edges: null`. `promote` refuses a `comparison` artifact, and manifests without a role count as deployable (design D9). Verify with tests:
  - an oversized export is refused for each role
  - `promote` on a comparison artifact leaves `CURRENT` unchanged and names the role
  - `promote` still works for the two existing roleless artifacts

  Est.: 45–75 min.
- [ ] 6.2 Export the compact winner as `deployable` and the large winner as `comparison`. Verify that both directories exist with measured `asset_bytes` within their budgets, `CURRENT` still reads `20260924T183944Z`, and `views.model_file` still serves the TabM. Est.: 20–30 min.
- [x] 6.3 Add `--artifact PATH` to `dbrel ml predict-delay`, using `DelayModel(PATH)` instead of `DelayModel.current(...)`, and update its help text to describe median output. Verify with a CLI test: predicting with a comparison artifact prints a median and band, the output differs from `CURRENT`'s for the same call, and `CURRENT` is not read. Est.: 30–45 min.
- [x] 6.4 Add the `arrival-net` + `quantiles` branch to `bahn-buddy-predict/src/lib/tabm.ts` (rows × 3 → p25/p50/p75, rule `median`). Extend `scripts/browser_check` to this output. Verify that the browser check matches the Python predictor bit for bit on its sample inputs, and that `bun run build` succeeds. Est.: 45–90 min.
- [ ] 6.5 Update the live accuracy study and its skill doc so the estimate is labelled a median again. Let the study take an optional artifact path, so the large model and the TabM can be run through it too. Verify that `tests/test_live_accuracy.py` passes, and that a study run on the new compact artifact labels "median". Est.: 30–45 min.
- [ ] 6.6 Present the report (compact vs large vs TabM vs window MLP: scores, sizes, per-month table, ablation result, most-frequent-value share) and ask for deployment approval of the compact model. Only on approval, run `promote`. Verify that `CURRENT` points to the new stamp, the site shows varied median estimates for a station board, and rolling back with `promote` to `20260924T183944Z` works. Est.: 15–30 min plus user review.
