# Tasks

Standing note for every task: first check whether it needs the current model. Delegate pure
execution to a Haiku subagent: installs, long-running commands and monitoring them. Delegate
correctness-sensitive but routine work to Sonnet. While any background run is pending, state
how long it is expected to take.

## 0. Review gate

- [x] 0.1 Walk Anas through the design.md D1 feature table and the D6 hyperparameter search
      spaces. Get his OK or edits before any code is written. Verify: his answer is recorded in
      design.md, with changes applied.

## 1. Setup

- [x] 1.1 Extend the `ml` extra in `pyproject.toml` with `catboost>=1.2`, `xgboost>=2.1` and
      `optuna>=4`, and install it into `.venv` with `uv sync --extra ml`
      [haiku]. Verify: `.venv/bin/python -c "import lightgbm, catboost, xgboost, optuna"` succeeds.
- [x] 1.2 Create the `src/dbrel/ml/` package and a lazily imported `ml` Typer sub-app in `cli.py`.
      Verify: `dbrel --help` works, and `dbrel ml --help` lists the four commands as stubs.

- [x] 1.3 GPU setup, step 1 (D10): ask Anas to run `! sudo pacman -S rocm-hip-sdk rocm-hip-runtime`.
      Verify: `pacman -Q hip-runtime-amd rocm-hip-sdk` shows 7.2.4.
- [x] 1.4 GPU setup, step 2: HIP smoke test on gfx1201. Verify: `hipInfo` (or a trivial HIP kernel)
      lists the RX 9070 XT as gfx1201 and runs without error. If it fails, report the error to
      Anas, mark XGBoost as CPU-fallback, and skip 1.5.
- [x] 1.5 GPU setup, step 3: write `scripts/build_xgboost_rocm.sh` to build XGBoost with
      `USE_HIP=ON`, `CMAKE_HIP_ARCHITECTURES=gfx1201`, and install the wheel into `.venv`
      [haiku to run the build once the script exists; ~20–40 min]. Verify:
      `python -c "import xgboost; print(xgboost.build_info())"` reports HIP enabled.

## 2. Features and dataset

- [x] 2.1 Implement the shared feature-row builder: every D1 feature, the unknown-category code,
      and `NaN` for a missing stop rank. Verify: unit tests on a fixture train cover sched time,
      start-time wrap past midnight, stop fraction, origin/terminus and direction (line and
      line-less).
- [x] 2.2 Implement the prediction-side row assembly from the timetable (`station_stop`, `train`,
      `station`, holiday calendar), with no `trip_stop` or averages access. Verify: a test builds
      a historical stop's row through both paths and asserts equality, and a test asserts
      that no query touches `trip_stop`, `stop_delay_average` or `train_stop_delay_average`.
- [x] 2.3 Implement `dbrel ml build-dataset`. It applies the D2 filters and writes `data/ml/dataset.npz`
      plus `dataset.json` (vocabularies, dates, row counts, DB mtime/size). Verify, in tests:
      a cancelled stop yields no target; an implausible arrival keeps its departure target; no
      live-only column appears among the features.
- [x] 2.4 Run `dbrel ml build-dataset` on the live DB [haiku; ~15–30 min]. Verify: `dataset.json`
      reports ~11M departure / ~11M arrival targets over 2026-08-24 … 09-22.

## 3. Split, baselines, metrics

- [x] 3.1 Implement the chronological date split (D4) and the metrics (D7): MAE, pinball, band
      coverage, coverage share, per-category / per-holiday / per-day breakdowns. Verify: unit
      tests confirm the sets are disjoint and in order, and that no run spans two sets; pinball
      and coverage are checked on hand-computed examples.
- [x] 3.2 Implement the averages baseline, keyed from training days only, and the global-median
      baseline. Verify: a test shows a key seen only on test days is "not covered" and gets no
      value, and that the stored averages tables are never read.

## 4. Candidates and tuning

- [x] 4.1 Implement the LightGBM candidate: 3 quantile boosters per side, native categoricals,
      early stopping, sorted outputs, raw crossing rate logged. Verify: a smoke test on a tiny
      synthetic in-memory frame (never written to the DB) gives ordered percentiles.
- [x] 4.2 Implement the CatBoost candidate (`MultiQuantile`) with the same interface. Verify: the same
      smoke test passes.
- [x] 4.3 Implement the XGBoost candidate (`reg:quantileerror`, multi-α, `enable_categorical`,
      `device="cuda"` on the HIP build with an automatic CPU fallback that is recorded; with the GPU it
      runs alongside the CPU libraries by default, on a 2 / 14 thread split, with `--no-parallel-gpu`
      to opt out) with the
      same interface. Verify: the same smoke test passes.
- [x] 4.4 Implement the Optuna tuner: D6 search spaces as editable constants, per-library time budget,
      3M-row trial sampling, median pruner, "under-tuned" flag below 5 finished trials. Verify:
      a tuner run with `--time-budget 1` on the smoke frame finishes and reports its trial count.

- [x] 4.5 GPU correctness check, D10 step 4: on ~1M dataset rows, fit the XGBoost quantile model on the
      GPU and on the CPU. Verify: validation pinball losses agree within 1%, no categorical
      errors, and both timings recorded. If this fails, fall back to CPU and report the reason to
      Anas.

## 5. Bake-off, artifacts, report

- [x] 5.1 Implement `dbrel ml bakeoff`:
      - tune → final fit → validation-based winner per side → test scoring of all candidates and
        baselines
      - atomic artifact directory plus `CURRENT` pointer
      - `manifest.json` and `report.md`
      Verify: a test on the smoke frame shows a second run creates a new directory, leaves the
      first untouched, and moves `CURRENT` only at the end.
- [x] 5.2 Implement `dbrel ml report [ARTIFACT]`. Verify: it reprints the table from a saved
      manifest.

## 6. Prediction CLI

- [x] 6.1 Implement `dbrel ml predict-delay` (`--train` or `--line`/`--direction`, `--station`,
      `--date`, `--time`). It prints scheduled time and `p50 (p25–p75)` per side, and prints no
      departure figure for a terminating call. Verify with tests:
      - it prints figures with the averages tables emptied
      - an unseen train×station combination still gets a prediction
      - a terminating call shows arrival only
      - with no model, it fails with a message naming `dbrel ml bakeoff`
- [x] 6.2 Point `predict-delay` at the TabM model (no more tree models): `dbrel.ml.nn` builds
      the inputs exactly as training did and runs the network through onnxruntime;
      `scripts/export_tabm.py` exports `nn_tabm_model.pt` to ONNX as a new artifact and moves
      `CURRENT` to it. Verify: the export's three checks pass (inputs equal training's, test MAE
      reproduced, ONNX equals PyTorch), and `predict-delay` gives plausible figures on 3 real
      calls (one ICE, one RE, one S-Bahn).

## 7. Wrap-up

- [x] 7.1 Run the full test suite. Verify: the existing 311 tests plus the new ones pass.
