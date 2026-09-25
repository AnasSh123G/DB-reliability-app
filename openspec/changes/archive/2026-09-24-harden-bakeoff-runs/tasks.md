# Tasks

Standing notes:
- Nothing in this change starts a full training run until task 6.3. Tasks 6.1–6.2 are short and
  capped.
- Every command that trains or allocates more than about 1 GB goes through `dbrel ml launch`,
  even during development. Free RAM is checked before each one.

## 1. Memory accounting

- [x] 1.1 Add `memory_report()` (D2): `VmRSS` + `VmSwap`, plus `memory.current`, `memory.peak`
      and `memory.swap.current` of the process's own cgroup. Replace every `rss_gb()` call in
      bakeoff/tune/tune_tabm with it, then delete `rss_gb()`. Correct the `release_memory()`
      docstring. Verify: a unit test allocates 200 MB and sees the total rise by ≥ 190 MB. On
      a non-systemd cgroup path, the group fields are `None` rather than an error.

## 2. Isolated, capped launcher

- [x] 2.1 Add `dbrel ml launch NAME [--memory 18G] -- CMD…` (D1):
      - `systemd-run --user --unit=dbrel-ml-NAME --slice=background.slice` with
        `MemoryMax`, `MemorySwapMax=0`, `OOMPolicy=stop`, `Nice=10` and `WorkingDirectory`,
        without `--collect`
      - refuse if the unit is active, and `reset-failed` a finished one
      - log to `data/ml/logs/NAME-<UTC stamp>.log`
      - print the unit, the log path and the stop command
      Verify: build + tests pass.
- [x] 2.2 Add `dbrel ml status NAME`, which shows active or finished, the exit reason
      (`Result=`, `ExecMainStatus`, `oom-kill`), memory peak and the log path. Verify manually
      and paste the output:
      - a `launch probe --memory 300M -- python -c "x = bytearray(600_000_000)"` ends with
        `oom-kill` in `status`, and `journalctl -k` shows no process killed outside that unit
      - `cat /proc/<pid>/cgroup` of a running launch shows `background.slice/dbrel-ml-…`, not
        `app-code-oss-*`
      - closing the terminal that launched a `sleep 120` unit leaves it running

## 3. Checkpoints and resume

- [x] 3.1 Implement the D3 checkpoint directory:
      - `key.json` built from the dataset (built_at + fingerprint), split, search-space hash,
        sample_rows and max_rounds
      - atomic `*.tmp` → rename writes, and the `done` marker
      - a mismatch fails with the differing fields
      Verify: a unit test shows a changed split or search space is refused by name, and a
      half-written `.tmp` is ignored.
- [x] 3.2 Make tuning resumable (D4):
      - Optuna SQLite storage with `load_if_exists`
      - remaining budget = budget − Σ finished trial durations
      - interrupted RUNNING trials marked FAIL and ignored
      - `history` rebuilt from the study
      Verify: a test runs 2 trials, then a second `tune()` call with the same storage runs
      only the remaining trials and budget.
- [x] 3.3 Restructure `_run_library` / `_prepare` (D5): per-side arrays built on demand and
      freed, `ds.features` dropped after re-coding, and each side saved via `Candidate.save()` +
      `.result.npz` + `.fit.json`, then deleted from memory. `bakeoff --resume` skips finished
      steps. Verify with tests on the smoke fixture, **killing a real subprocess** with SIGKILL:
      - killed after the departure-side checkpoint: the resume refits only the arrival side
        (asserted from the log and the file mtimes)
      - killed mid-tuning: the resume keeps the finished trials
      - the final report equals an uninterrupted run's candidates and winners
- [x] 3.4 Write the artifact by copying the winners' checkpoint model files. No model is
      loaded (D5). Verify: the existing artifact/CURRENT tests pass, and `predict-delay` loads
      the copied files.

## 4. Memory check

- [x] 4.1 Add `dbrel ml memory-check --libraries …` (D6). For each library it fits once at the
      3 M tuning sample and at 1 M and 2 M final-fit rows, using the search space's
      memory-hungriest corner (plus tuned params if any), with at most 100 rounds. It measures
      the peak from `memory.peak` and the time per round, projects both sides' full size with a
      15 % margin, and writes `data/ml/memory_check.json`. Verify: a test on the fixture writes
      the file with every field, and a fake projection above the cap makes `launch` for a
      bake-off refuse with library, projection and cap named. `--ignore-memory-check` overrides
      it and says so in the log.

## 5. TabM

- [x] 5.1 TabM (D10): `tune_tabm.py` logs `memory_report()`, and a `launch` of "bake-off
      then TabM" runs both in one unit, one after the other. Verify: a dry chain of two
      `sleep` commands runs in sequence under one unit, and `status` reports both exit codes in
      the log.

## 6. Checks

- [x] 6.1 Full test suite (`pytest`). Verify: everything passes, with no conversion code or
      test left.
