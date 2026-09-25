# Design

## Context

See proposal.md (Why). Everything below was checked read-only on 2026-09-24, with no training
running.

**Machine**
- 16 threads and 30 GB RAM.
- Swap is 31 GB of **zram**, which is compressed RAM, not disk.
- systemd 261. The user manager is delegated the `cpu io memory pids` controllers, so
  `MemoryMax=` / `MemorySwapMax=` on a user unit are enforced.

**Dataset**
- 12,079,413 rows over 2026-08-21..09-22: 7.24 M training rows on the departure side and
  7.19 M on the arrival side.
- 18 features. Vocabularies: train 66,121, station 5,260, line 780.
- The database fingerprint still matches the dataset, so it is not stale.
- `dbrel serve`, which is running now, only reads the database.

**The three kills** (kernel log, `journalctl -k`):

| time | dbrel RSS | dbrel swapped | what was running |
|---|---|---|---|
| 13:12 | 5.2 GB | – | CatBoost tuning, alongside a GPU diagnostic script of mine that held full-batch tensors |
| 14:09 | 16.8 GB | – | CatBoost tuning, before `release_memory()` existed |
| 17:10 | 13.3 GB | **21.9 GB** | CatBoost arrival-side final fit |

At 17:10 the rest of the desktop (Brave, Code-OSS, Claude, Steam and so on) totalled under 3 GB.

**Bugs found in the current code:**
- **B1 · `rss_gb()` misreports memory.** It reads `VmRSS` only, so pages pushed to zram look
  freed. The "memory fixed, 6–11 GB" conclusion and the `release_memory()` docstring rest on it.
  Whether a leak across trials remains is therefore **unknown, not fixed**.
- **B2 · Results are cached per library, after both sides.** A kill loses the tuning (up to
  60 min) and every finished side.
- **B3 · The departure-side model stays in memory during the arrival-side fit.**
  `record["models"]["dep"]` keeps the fitted CatBoost model alive during the second-largest
  allocation of the run.
- **B4 · `--reuse` unpickles the whole 3.1 GB `lightgbm.pkl`,** boosters included, only to set
  `models = None`. That spikes memory before CatBoost starts.
- **B5 · The artifact step unpickles the same file again** (`_load_cached(...)["models"]`) for
  the winner, after all training. A kill there loses the report.
- **B6 · `_prepare` keeps the dataset twice.** Both sides' train/validation/test matrices live for
  the whole run, and `ds.features` (original codes) stays alive next to the re-coded copy. That
  is roughly 2–3 GB of baseline that no library needs at once.
- **B7 · The launch escaped the terminal but not the editor.** `setsid`/`disown` detach from the
  terminal, not from the `app-code-oss-*.scope` cgroup, and my `systemd-run --collect` launch
  deleted the unit on exit, so its exit reason was lost. Every launch also overwrote
  `bakeoff-full.log`.
- **B8 · XGBoost is the AMD ROCm fork** (`amd_xgboost` 3.2.0, built with `USE_HIP`). GPU use is
  ruled out (7.19 % mismatch). Its CPU path was never checked against upstream. Because the
  distribution name differs from the `xgboost` in `pyproject.toml`, a `uv sync` would install
  PyPI xgboost over the same `xgboost/` package directory.
- **B9 · XGBoost on the CPU is slow here.** The GPU check's CPU run took 502 s for 1 M rows ×
  300 rounds on 2 threads. At 3 M sample rows and 14 threads, a single trial with early stopping
  can take tens of minutes, so the 60-minute budget yields only about 1–3 trials, and those are
  flagged under-tuned.

The 31 existing ML tests pass (9 s). None of them covers memory, a kill or a resume.

**Checked and fine:**
- the staleness check
- `serve` being read-only
- the GPU path being off for every library
- the TabM script's host memory: about 1.6 GB of arrays, which go to VRAM once, plus the 2 GiB
  batch guard
- `tune()`'s deadline handling
- CatBoost's reporter returning False to stop
- XGBoost's `max_bin` agreement
- the chronological split and training-only vocabulary

## Goals / Non-Goals

**Goals:**
- No run can take the desktop down. Hitting the cap costs at most one step.
- Memory numbers that can be trusted, and a measured go/no-go before a multi-hour run.
- Comparing CatBoost and XGBoost on the test days from saved predictions only (LightGBM is out,
  see D9).

**Non-Goals:**
- Changing features, split, metrics, search spaces or the winner rule.
- Making CatBoost/XGBoost cheaper by changing how they model. Any such option goes to Anas
  (D7, D8).
- A general job scheduler.

## Decisions

### D1. Launch through a transient systemd user service
`dbrel ml launch -- <command>` wraps `systemd-run --user` with:
- `--unit=dbrel-ml-<name>` and `--slice=background.slice`
- `-p MemoryMax=<cap> -p MemorySwapMax=0 -p OOMPolicy=stop`
- `-p WorkingDirectory=<repo>` and `-p Nice=10`
- **no `--collect`**, so `systemctl --user status` and `journalctl --user -u` keep the exit
  reason (`oom-kill`, exit code, or success) after the service ends

The launcher:
- refuses a cap larger than `MemAvailable` minus 1 GiB (added during apply). A cap above free
  memory protects nothing: the machine runs out before the cap is reached, and the kernel then
  kills a process anywhere.
- refuses if a unit of the same name is active
- runs `reset-failed` on a finished one before reuse
- writes to `data/ml/logs/<name>-<UTC stamp>.log`, never overwriting
- prints the unit name, the log path and the stop command

The cgroup parent is the systemd user manager, so closing Code-OSS or any terminal, or killing
Claude, does not touch it (verified: `background.slice/…service`, PPID of the user manager).

- *Alternative considered: `setsid`/`nohup`.* Rejected. It leaves the process in the editor's
  cgroup (B7).
- *Alternative considered: `--scope`.* Rejected. A scope stays attached to the launching
  process's lifetime semantics.
- *Default cap: `MemoryMax=18G`.* The desktop idles at about 9 GB of RAM including zram's own
  footprint, so this leaves 3 GB of headroom. With `MemorySwapMax=0`, going over is a fast,
  clean kill of the job alone instead of an hour of zram thrashing followed by a global OOM.

### D2. Truthful memory accounting
`memory_report()` returns:
- `VmRSS` and `VmSwap` from `/proc/self/status`
- `memory.current`, `memory.peak` and `memory.swap.current` from the process's own cgroup
  (`/proc/self/cgroup` → `/sys/fs/cgroup/...`), when it is in a dedicated unit

Every trial and fit line logs `rss X + swap Y = Z GB, group peak P GB`. `rss_gb()` is deleted
rather than kept, since callers must not keep using the misleading number. The docstring claim
on `release_memory()` is corrected: it returns freed pages, but does not prove there is no leak.

### D3. Checkpoint layout, one directory per library
```
data/ml/runs/<library>/
  key.json              dataset built_at + fingerprint, split, search-space hash, sample_rows, max_rounds
  optuna.sqlite3        the study (Optuna RDB storage), written per trial
  tuning.json           TuningResult, once tuning is finished
  <side>.model.*        the side's fitted model in the library's own format (Candidate.save)
  <side>.result.npz     test predictions, validation pinball, crossed shares
  <side>.fit.json       describe(): params, rounds, fit seconds, device, threads
  done                  written last, once both sides exist
```
- Each file is written to `*.tmp` and renamed, so a kill mid-write leaves no half file.
- On start, `key.json` must match exactly. A mismatch fails with the differing fields (spec:
  "saved work from a different dataset is not reused"). A new dataset means new directories, and
  nothing is deleted automatically.
- *Alternative considered: keep one pickle per library.* Rejected. Pickling a whole booster is
  what makes B4 and B5 so large, and one file per library cannot express "tuning done, one side
  done".

### D4. Resumable tuning
`tune()` opens `optuna.create_study(storage="sqlite:///…/optuna.sqlite3",
study_name=<library>, load_if_exists=True)`.
- The remaining budget is `budget − Σ(duration of finished trials in the study)`.
- A trial that was running at the kill is marked FAIL on resume and ignored.
- The median pruner sees the stored intermediate values, so pruning stays consistent.
- The per-trial sample uses the same seeded row choice, so resumed trials see the same rows.
- `TuningResult.history` is rebuilt from the study, not from memory.

### D5. One side at a time, one side's arrays at a time
- `_prepare` keeps only the re-coded `features` and the targets. The full feature matrix is never
  built. The original categorical codes in `ds.features` (8 × 46 MiB) are dropped once re-coded
  when the caller opts in (`drop_raw_features`, set by the CLI), since tests reuse one dataset
  object across runs.
- A side's `X_train`/`X_val`/`X_test` are built on demand and freed after that side's fit and
  predictions.
- The tuning sample is built once per library, from the departure side.
- After each side: `Candidate.save()` to the checkpoint, keep only the predictions (about
  2.5 M × 3 float64 ≈ 60 MB per side), `del` the candidate, `release_memory()`.
- The artifact step **copies** the winners' saved model files into the artifact directory. No
  model is loaded to write the artifact (fixes B3, B5, B6).

### D6. `dbrel ml memory-check` before a full run
For each selected library, inside a capped unit, the check fits:
1. once at the tuning sample size (3 M rows)
2. once at 1 M rows and once at 2 M rows of the final-fit training set

Each fit uses **the most memory-hungry corner of that library's approved search space**. For
CatBoost that is depth 10 and border_count 254. For XGBoost it is max_depth 12, max_leaves 1023
and max_bin 512. For a library that has already tuned, the tuned parameters are used as well.

Rounds are capped at 100, which is enough to allocate the pools, CTRs and histograms. Model
growth per round is measured and extrapolated to the median rounds seen in tuning.
- The peak (from `memory.peak`) is fitted linearly in rows and projected to the full training
  size of each side.
- The check writes `data/ml/memory_check.json`. `launch` for a bake-off reads it and refuses if
  a projected peak plus 15 % margin exceeds the cap (`--ignore-memory-check` overrides, and says
  so in the log).
- The check takes minutes, not hours, and runs under the same cap, so it cannot hurt the
  desktop.

### D7. If CatBoost's final fit does not fit under the cap, Anas decides
The memory-check result decides whether this is needed at all. Options, in order of how little
they change the model:
- (a) set CatBoost's `used_ram_limit` near the cap, which only bounds CTR computation memory
- (b) lower `max_ctr_complexity` from its CPU default of 4 to 2 or 1, which changes which feature
  combinations it may use
- (c) do the final fit on a sample, like tuning

(b) and (c) are modelling choices and are not taken without his approval.

### D8. XGBoost: upstream build and a realistic budget, Anas decides
- Recommendation: uninstall `amd_xgboost` and install PyPI `xgboost`, which matches
  `pyproject.toml` (fixes B8). The fork's only benefit was the GPU, which is ruled out.
- On speed (B9), the options are:
  - accept 1–3 trials, flagged under-tuned in the report
  - give XGBoost a longer budget
  - skip XGBoost for this report

  memory-check also measures XGBoost's per-round time, so the choice is made with numbers.

### D9. LightGBM is left out, and its old result is not converted
The model is meant to run in a browser, and Anas set the limit at 35 MB. LightGBM's fitted
model is about 3 GB, so it cannot be used, and it is not part of this comparison. Its old
result file (`data/ml/runs/lightgbm.pkl`, 3.1 GB) is not loaded or converted by anything. It
stays on disk until Anas deletes it.

A conversion step was built and then removed on Anas's decision (2026-09-24). Loading the
pickle alone peaked at 10.8 GiB, and checking the converted models meant predicting 2.5 M test
rows with them.

How candidates are compared, for every library (D5): right after a side's final fit, the model
predicts the validation and test days. Those predictions are saved, and the model is freed.
The comparison and the report read only the saved predictions, never a model.

### D10. TabM search under the same rules
- `scripts/tune_tabm.py` uses `memory_report()` and runs through `launch` with its own cap
  (default 12G; VRAM is separate).
- The chain "TabM after the bake-off" becomes one launched unit that runs the two commands in
  sequence, so they never overlap. It replaces the old `until grep …` watcher shell.
- Persisting TabM's Optuna study is out of scope. Its 2-hour search is restartable as a whole,
  and TabM is an experiment script.

## Risks / Trade-offs

- **The linear projection underestimates CatBoost's CTR memory, which can grow super-linearly
  with rows.** → There are two probe sizes plus a 15 % margin. The hard cap still makes an
  underestimate cost one checkpointed step, not the desktop.
- **`MemorySwapMax=0` kills a run that would have finished with some swapping.** → That is the
  intended trade: a quick, contained failure with a measured number, rather than hours of zram
  thrashing and a global OOM.
- **Optuna's SQLite study and a kill mid-write.** → Optuna commits per trial, and an interrupted
  RUNNING trial is marked FAIL on resume.
- **The resumed run's timing differs from an uninterrupted one.** → Timing is reported per step,
  so nothing claims otherwise.

## Migration Plan

1. Land D2–D6 with tests, then run the full ML test suite.
2. Anas decides D8. If PyPI XGBoost is chosen, reinstall it.
3. `dbrel ml memory-check --libraries catboost,xgboost` (capped). Anas sees the projections and
   decides D7 only if CatBoost does not fit.
4. `dbrel ml launch bakeoff -- dbrel ml bakeoff --libraries catboost,xgboost`, then TabM in the
   same unit.

Rollback: the checkpoint directories are new and separate from everything else, and code
changes revert with git.
