# Proposal

## Why

The bake-off was killed by the kernel's out-of-memory killer three times on 2026-09-24 (13:12,
14:09, 17:10), and about four hours of CPU work were lost. The kernel's own records show the
cause was the bake-off process, not the editor:
- At 17:10 the process held 13.3 GB in RAM plus 21.9 GB swapped into zram, about 35 GB in all.
  The machine has 30 GB, and zram swap is itself compressed RAM, so there is no extra room.
- The run's own "process memory" log line counts only resident pages. Memory being pushed into
  swap therefore looked like memory being freed (11 → 5 GB), and the growth went unnoticed.
- Each kill lost everything since the last library had finished. That was about 55 minutes of
  CatBoost tuning and final fit, because a library's result is only saved once both sides are
  done.
- The jobs also ran inside the editor's process tree and cgroup, so they shared its fate and its
  memory accounting.

A run must not be started again until it is capped, measurable and resumable.

## What Changes

- **Isolated launch.** A bake-off, and the TabM search after it, run as a systemd user service
  outside the editor's and the terminal's process tree and cgroup. The service has a hard memory
  cap and no swap. Hitting the cap kills only that service, never the desktop, and the reason
  stays readable afterwards.
- **True memory reporting.** The run logs RAM + swap + its cgroup's peak, not resident memory
  alone.
- **Checkpoint and resume.** Work is saved after each step, per library:
  - the tuning study, trial by trial
  - each side's final model, saved in the library's own file format
  - each side's predictions

  A resumed run skips finished steps. An interruption costs at most the step in progress.
- **No model kept in memory it isn't using.** A finished side's model is saved and dropped
  before the next fit. Reused or winning models are copied as files and never reloaded into
  memory. Today the 3 GB LightGBM pickle is fully unpickled at the start of a reuse run, and
  again when the artifact is written.
- **A measured memory check before any full run.** A short, capped probe measures each library's
  peak memory at the tuning and final-fit sizes. The full run is only launched if the projected
  peak fits under the cap.
- **LightGBM is left out of the comparison.** Its model is about 3 GB, far over the 35 MB a
  browser model can be. Its old result file is not loaded or converted by anything.
- **Decisions put to Anas before the run:**
  - put back the upstream PyPI XGBoost in place of the AMD ROCm fork. GPU XGBoost is ruled out,
    and the fork is also a `uv sync` hazard.
  - what to do if CatBoost's final fit does not fit under the cap
  - whether XGBoost's CPU speed makes its 60-minute budget meaningful

## Capabilities

### New Capabilities
- `delay-model` (requirements added): runs are isolated and memory-capped, memory is reported
  truthfully, and runs resume from checkpoints. The capability itself is introduced by the
  in-flight change `add-offline-delay-model`. This change only adds requirements to it and
  changes none of that change's requirements.

### Modified Capabilities
(none in `openspec/specs/`)

## Impact

- **Code:**
  - `src/dbrel/ml/bakeoff.py`: checkpoint layout, memory accounting, per-side release, artifact
    copying
  - `src/dbrel/ml/tune.py`: persistent Optuna study and budget accounting across a resume
  - `src/dbrel/ml/cli.py`: `bakeoff --resume`, a new `dbrel ml memory-check`, and a new
    `dbrel ml launch` that starts the capped systemd service
  - `scripts/tune_tabm.py`: the same launcher and memory accounting
- **Data:** `data/ml/runs/` moves from one pickle per library to a directory per library. The
  old `lightgbm.pkl` is left untouched and unused.
- **Dependencies:** possibly switch XGBoost from `amd_xgboost` back to PyPI `xgboost`, pending
  Anas's decision. No new packages: Optuna's SQLite storage uses the installed SQLAlchemy.
- **System:** `systemd-run --user` (systemd 261; the memory controller is delegated to the user
  manager, verified). No root, and no change to system configuration.
- **Not affected:** features, dataset, split, metrics, search spaces, and prediction.
