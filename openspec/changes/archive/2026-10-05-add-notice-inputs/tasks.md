# Tasks

Before every task, tell the user its current duration estimate and the basis for it. Keep
hands-on work separate from unattended computation, and revise the estimates once measurements
change them. The ranges below are provisional.

Groups 1–5 use fixtures only. Group 6 touches the real database and the GPU, and each step in it
needs the user's go-ahead. Nothing moves `CURRENT` without the user's approval (the full-year
change's task 6.6). This change builds on `rebuild-arrival-model-for-full-year` and runs before
its tasks 5.2 and 5.3.

## 1. The daily notice table and its builder

- [x] 1.1 Add the `NoticeRateDaily` model (level, key_id, service_date, n, k_works,
  k_disruption; primary key on the first three) to `models.py`, created by `create_all()`. Verify
  with a test that a fresh database has the table and that the schema migration leaves an existing
  database unchanged. Est.: 30 min.
- [x] 1.2 Add `derive/notice_rates.py`: per service date, one transaction, replace that date's
  rows; classify a notice by its category prefix ("Bauarbeiten" versus "Störung"/"Großstörung",
  `msg_type = 'h'` only); a notice counts when it has a non-deleted version; count only arrivals
  that pass the dataset's eligibility filter. Verify with tests on a small fixture covering: a
  trip with both kinds, a trip whose only notice is deleted, an ineligible arrival that is not
  counted, a trip without a line (line level skipped), and a second run that changes nothing.
  Est.: 2 h.
- [x] 1.3 Add `dbrel ml build-notice-rates [--from --to]` with a progress line every 15th and 1st
  of the month and a per-month notices-per-1,000-trips summary that flags a month more than 3×
  below the median. Verify with a CLI test on the fixture (output lists the months and the
  flag). Est.: 1 h.

## 2. The rate lookup

- [x] 2.1 Add `ml/notice_rates.py` with a `NoticeRates` class built from the table and a list of
  training dates: dense per-key totals, per-month totals, the `(k + w·parent)/(n + w)` smoothing
  with `w = 20`, NaN for `n = 0`, and a `rates(station_id, line_id, train_id, day, fold)` lookup
  where training rows use `total − month` and everything else uses `total`. Verify with unit tests
  for the spec's scenarios: a busy station's rate, a rare train near its line, an unseen train is
  NaN, a train without a line, and a training row on 2026-03-14 unaffected by an added March
  arrival while a validation row is affected only by training dates. Est.: 2.5 h.
- [x] 2.2 Add the manifest record and fingerprint (`dates`, `weight`, hash of the counts over those dates) and a check that refuses on mismatch. Verify with a test
  that changing one count in the table makes loading fail with a message naming the mismatch.
  Est.: 1 h.

## 3. Features and prep

- [x] 3.1 Add `NOTICE_RATES` and the full numeric set to `features.py`, with `with-notice`,
  `no-notice-rates` and `no-doy` in `NUMERIC_SETS` of `full_year_search.py` (default
  `with-notice`). Verify with `tests/test_ml_features.py` that the existing sets are unchanged and
  with a test that the prep layout adds the six columns plus a `_missing` column only where
  training saw NaN. Est.: 1 h.
- [x] 3.2 Compute the rate columns on read in `full_history`/`full_year`: training rows use the
  leave-one-month-out fold, validation and test rows the all-training-dates rates; add the rates
  fingerprint to the prepared-set key. Verify with tests that (a) rates for the same arrival
  differ between the training fold and the validation lookup, (b) changing the table changes the
  key and the prepared inputs are rebuilt, not reused, and (c) a two-month fixture's training
  rows in month 1 use no month-1 data. Est.: 3 h.
- [x] 3.3 Update `training_statistics` and `prep_from_statistics` so the rate inputs are
  standardised from the training rows like the other numerics. Verify with the existing
  streamed-versus-in-memory prep equality test extended to the new set. Est.: 1 h.

## 4. Search and report

- [x] 4.1 Make the compact track's variant step produce two variants (`no-doy`,
  `no-notice-rates`) and list both beside the winner in `ranking.json` and `final.json`. Verify
  with `tests/test_ml_full_year_search.py` on the fixture: both ablation trials trained, ranked on
  the same rows, and shown in the report. Est.: 2 h.
- [x] 4.2 Account for the notice rates in the compact memory check: the probes measure the wider
  numeric block themselves, so the check adds the rate tables' size (training dates for the search,
  training + validation for the refit), which the probes' few dates don't hold, to both projected
  peaks. Verify with the memory-check fixture test. Est.: 45 min.

## 5. Serving and the offline path

- [x] 5.1 Build the rates in `_model_input` and the offline prediction path only when the
  artifact's `features_numeric` lists them, loading the arrays once per process and checking the
  fingerprint. Verify with a test that the encoded arrays from both paths are identical for the
  same call, that an older artifact never reads the table, and that no network call is made.
  Est.: 2.5 h.
- [x] 5.2 Document the table, the command and the leakage rule in the ML README, and add the new
  inputs to the model-input documentation. Verify that every documented command runs as written
  on the fixture. Est.: 45 min.

## 6. Real-data steps (each needs the user's go-ahead)

- [x] 6.1 Run `dbrel ml build-notice-rates` on the database as a capped job
  (`dbrel ml launch notice-rates --memory 4G`). Verify the table row count and that the month
  summary shows no flagged month; record the peak memory (RSS plus swap). Then load
  `NoticeRates` for the training dates once and record the load time and resident size (the
  first prediction after a server restart pays this). Est.: 10 min hands-on plus about 10 min
  unattended.
  Result, 2026-10-01: 13.0 M rows (station 1.68 M, line 0.22 M, train 11.15 M) over 330 dates;
  99,566,282 arrivals, equal to the dataset's rows, and every month's notice density equal to the
  2026-10-01 read-only measurement; no month flagged. 19.4 min (not 10), RSS 0.3 GiB, group peak
  4.0 GiB (page cache against the cap; not killed). The first run failed at once because the real
  database had no table yet, so the builder now creates its own table. Loading the rates for the
  training dates: 22 s, 91 MiB of counts, 575 MiB peak RSS.
- [x] 6.2 Check `df` for the prepared sets (see design: Disk). Then rerun the compact memory
  check (`search_full_year.py memory-check --track compact`, capped with `--oom-policy
  continue`) with the new numeric set and record the projected peak. Verify it is under the
  14 GiB cap and that `notice_rates_bytes` is in `memory_check-compact.json`. Est.: 10 min
  hands-on plus about 15 min unattended.
  Result, 2026-10-02: probes 2.57 GiB at 1.04 M rows and 2.55 GiB at 2.03 M rows (33 s and 30 s);
  rate tables 91 MiB (search) and 103 MiB (refit) on top; projected peak 2.83 GB for the search
  and 2.84 GB for the final refit, against the 14 GiB cap. The check took 2 min, not 25. The
  projection is flat because the two probe peaks are equal (the peak is fixed overhead, not
  rows), which matches the 2.1 GiB RSS of the earlier full-size dry run; the 6.1 GiB projection
  before this change came from a positive slope between its probes. The launch gate now accepts
  the compact track.
- [x] 6.3 Move the old `runs/compact-2026-09-26` aside (its checkpoint key no longer matches the
  new feature set) and continue with the full-year change's 5.2 (dry run) and 5.3 (compact
  track) using the default `with-notice` set. Verify that `ranking.json` lists the `no-notice-rates` and `no-doy`
  ablations, and report the notice rates' effect on validation average error. Est.: as in that
  change, plus about 15–25 min for the extra trial.
  Result, 2026-10-02: compact track finished (8 trials, MLP 1024×3 skipped by budget). The
  `no-notice-rates` ablation of the best fixed trial (MLP 512×3, train embedding 32) scored 2.661
  against 2.660 with every input (+0.001 min): no measurable effect of the notice rates on
  validation MAE. The `no-doy` ablation scored 2.590 (−0.070), within the 0.1–0.2 min
  epoch-to-epoch noise of single runs.
