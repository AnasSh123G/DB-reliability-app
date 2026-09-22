# Tasks

## 1. Delete ML subpackage and tests

- [x] 1.1 Delete all 7 files in `src/dbrel/ml/` (`artifacts.py`, `_common.py`, `conformal.py`, `evaluate.py`, `features.py`, `live.py`, `train.py`) and the `__init__.py`; verify the directory is gone
- [x] 1.2 Delete the 6 ML test files (`test_ml_artifacts.py`, `test_ml_conformal.py`, `test_ml_evaluate.py`, `test_ml_features.py`, `test_ml_live.py`, `test_ml_train.py`); verify `pytest tests/` passes
- [x] 1.3 Remove any remaining references to `dbrel.ml` in `cli.py`, `collect.py`, or any other file; verify `grep -r "dbrel.ml" src/` returns nothing

## 2. Create `ingest/` and migrate `timetables/`

- [x] 2.1 Create `src/dbrel/ingest/` with an empty `__init__.py`; verify the directory exists
- [x] 2.2 Move `timetables/changes_poller.py`, `client.py`, `parser.py`, `plan_import.py`, `ratelimit.py`, `runs.py`, `stations.py` into `ingest/`; delete the now-empty `timetables/` directory
- [x] 2.3 Update all `from dbrel.timetables.X import ...` and `import dbrel.timetables.X` references in `cli.py`, `collect.py`, `db.py`, `web/queries.py`, `web/views.py`, and any test files to use `dbrel.ingest.X`; verify `grep -r "dbrel.timetables" src/ tests/` returns nothing
- [x] 2.4 Run `pytest tests/` and confirm all surviving tests pass

## 3. Migrate `gtfs/` into `ingest/`

- [x] 3.1 Move `gtfs/download.py`, `gtfs/static_import.py`, `gtfs/timeutil.py` into `ingest/`; delete the now-empty `gtfs/` directory
- [x] 3.2 Update all `from dbrel.gtfs.X import ...` references throughout `src/` and `tests/` to use `dbrel.ingest.X`; verify `grep -r "dbrel.gtfs" src/ tests/` returns nothing
- [x] 3.3 Run `pytest tests/` and confirm all surviving tests pass

## 4. Absorb `derive/` into `ingest/`

- [x] 4.1 Move `sched_sec_of` from `derive/_common.py` into `ingest/timeutil.py` (append to the bottom of that file); delete `derive/_common.py`
- [x] 4.2 Move `derive/finalize_tt.py` into `ingest/finalize_tt.py`; update its internal import of `sched_sec_of` to come from `dbrel.ingest.timeutil`
- [x] 4.3 Delete the now-empty `derive/` directory; verify `grep -r "dbrel.derive" src/ tests/` returns nothing
- [x] 4.4 Run `pytest tests/` and confirm all surviving tests pass

## 5. Flatten `models/` into a single `models.py`

- [x] 5.1 Create `src/dbrel/models.py` containing all ORM model definitions: copy content from `models/core.py`, `models/meta.py`, `models/timetables.py`, and the two constants from `models/aggregates.py` into one file; verify all names previously exported by `models/__init__.py` are present
- [x] 5.2 Delete `src/dbrel/models/` (all 5 files: `__init__.py`, `core.py`, `meta.py`, `timetables.py`, `aggregates.py`); verify the directory is gone and `src/dbrel/models.py` exists
- [x] 5.3 Verify callers are unaffected: `from dbrel.models import Station` still resolves because the public name `dbrel.models` is preserved; run `pytest tests/` to confirm

## 6. Final verification

- [x] 6.1 Run `grep -r "dbrel\.timetables\|dbrel\.gtfs\|dbrel\.derive\|dbrel\.ml\|dbrel\.models\." src/ tests/` and confirm zero matches (all old import paths are gone)
- [x] 6.2 Run the full test suite `pytest tests/ -v` and confirm all surviving tests pass with no errors or import failures
- [x] 6.3 Confirm the final package layout: `src/dbrel/` contains only `ingest/` and `web/` as subpackages, plus flat `.py` files at the top level
