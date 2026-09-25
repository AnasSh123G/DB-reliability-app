# Proposal

## Why

Every delay figure the app shows today is a lookup in `stop_delay_average` /
`train_stop_delay_average`. A lookup is only as good as its exact key. A combination the table
has never seen gets no figure at all, for example a new train, a rarely-run time, or a holiday
at a small station. Figures resting on two or three samples swing wildly. A model trained on the
14.3M observed stops (30 full days, 5,422 stations) can generalise across similar stations,
lines, times and route positions. It can then predict any scheduled call from the timetable
alone, "offline", with no averages table read at prediction time. The ML pipeline was deleted on
2026-09-22 so it could be redesigned for national scale. That scale is now in the database.

## What Changes

- Build one training dataset from recorded, non-cancelled stops. Each row carries only features
  that are knowable from the timetable before the train runs:
  - identity: station, train, line, category, operator
  - calendar: weekday, holiday flag
  - scheduled time and trip start time
  - location along the route
  - geography: coordinates and state
  - direction
- Split the data by whole service dates into train, validation and test.
- Run a **bake-off between LightGBM (CPU), CatBoost (CPU) and XGBoost (AMD GPU via ROCm, with a
  CPU fallback)**:
  - Each library is tuned with a time-boxed Optuna search on the validation days.
  - Each predicts the arrival and departure delay **median plus a p25–p75 band**.
  - All three are scored once on the untouched test days, next to two baselines: averages built
    from the training days only, and a global median.
- Save the winning model, plus every candidate's scores, as a versioned artifact under
  `data/models/`.
- Add CLI commands to train and evaluate, and **`dbrel predict-delay`**, which prints a train's
  predicted arrival/departure delays at a station on a date from the model and the timetable
  alone.
- The web UI does **not** change in this change. It keeps showing historical averages. Swapping
  it over is a later decision, made once the accuracy numbers are in.

## Capabilities

### New Capabilities
- `delay-model`: training, evaluating and querying an offline delay-prediction model. Covers the
  leakage-free feature set, the date-based split, the model bake-off and baselines, the saved
  artifact, and schedule-only prediction from the CLI.

### Modified Capabilities
(none. `stop-delay-averages` and `web-frontend` keep their current behaviour.)

## Impact

- **New code**: `src/dbrel/ml/`, the only package allowed to import numpy/lightgbm/catboost/
  xgboost/optuna, and new commands in `src/dbrel/cli.py`.
- **Dependencies**: the optional `ml` extra grows from `lightgbm, numpy` to add `catboost`,
  `optuna`, plus XGBoost built from source against ROCm/HIP for the RX 9070 XT (gfx1201). This
  needs the system packages `rocm-hip-sdk` / `rocm-hip-runtime` (7.2.4, official repos).
  That is a few hundred MB of wheels and system packages. The collector and web installs are
  unaffected, because the extra stays optional.
- **Data**: the database is only read, never changed. Model artifacts (tens of MB per candidate)
  are written to `data/models/v2/<timestamp>/`. The stale v1 artifacts from the deleted pipeline
  are left alone.
- **Compute**: one full bake-off is expected to take a few hours (Ryzen 7 7700, 8 cores / 16
  threads, plus the GPU for XGBoost). Most of that is tuning time, time-boxed per library.
