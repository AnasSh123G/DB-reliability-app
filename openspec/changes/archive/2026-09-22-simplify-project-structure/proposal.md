# Proposal

## Why

The project has accumulated too many files and layers of abstraction across 6 subpackages, making it harder to navigate and reason about than the domain actually demands. The goal is to trim it back to something that reads like a focused university project: flat, legible, and no more structure than the code earns.

## What Changes

- **BREAKING: Remove ML subsystem** — delete `src/dbrel/ml/` entirely (7 files: `artifacts.py`, `_common.py`, `conformal.py`, `evaluate.py`, `features.py`, `live.py`, `train.py`) and all ML-related tests (`test_ml_artifacts.py`, `test_ml_conformal.py`, `test_ml_evaluate.py`, `test_ml_features.py`, `test_ml_live.py`, `test_ml_train.py`). The ML pipeline will be redesigned from scratch later.
- **Merge `timetables/` and `gtfs/` into `ingest/`** — combine the two data-ingestion subpackages into a single `src/dbrel/ingest/` package. No behavior changes; files are reorganized and imports updated throughout.
- **Flatten `models/`** — the `models/` subpackage has 4 files (`core.py`, `aggregates.py`, `meta.py`, `timetables.py`); collapse into fewer files or move into the top-level package directly.
- **Remove `derive/` subpackage** — `derive/` contains only `finalize_tt.py` and `_common.py`; move the relevant logic closer to where it's used (into `ingest/` or top-level).
- **Consolidate `web/` templatetags** — `web/templatetags/` contains a single file (`versioned_static.py`); inline or keep flat, removing the extra folder layer if practical.
- Remove `views.py` at the top level if it is a duplicate/stub of `web/views.py`.

## Capabilities

### New Capabilities
_None — no new behaviors are introduced._

### Modified Capabilities
_None — behaviors of the surviving subsystems are unchanged. This change is a pure structural refactor; `skip_specs: true` is set._

## Impact

- **Deleted**: `src/dbrel/ml/` (entire subpackage), 6 ML test files, `src/dbrel/derive/`
- **Reorganized**: `src/dbrel/timetables/` + `src/dbrel/gtfs/` → `src/dbrel/ingest/`; `src/dbrel/models/` flattened
- **Import paths change** everywhere that references the moved/removed modules — `cli.py`, `collect.py`, `db.py`, `web/views.py`, `web/queries.py`, remaining tests, `migrations/`
- **No API or database schema changes**; Alembic migrations and the SQLite schema are untouched
