# Design

## Context

See proposal.md — Why.

Current layout has 6 subpackages plus a `derive/` utility layer. Many of these subpackages hold 2–3 small files whose only purpose is to keep the namespaces separated — which was reasonable when the ML pipeline needed a strict boundary, but is now unnecessary overhead.

Key observations from reading the code:
- `models/` is 4 files (`core.py` 466 lines, `meta.py` 84, `timetables.py` 140, `aggregates.py` 19) re-exported through an `__init__.py`. A single `models.py` holds all of this comfortably.
- `derive/_common.py` only exists to share `sched_sec_of` and `percentile` between `derive/finalize_tt.py` and the ML pipeline. With ML gone, `percentile` is deleted and `sched_sec_of` moves into `ingest/timeutil.py` next to the `epoch_to_gtfs_sec` it already calls.
- `gtfs/` and `timetables/` are both data-ingestion: one fetches live/planned departure data, the other fetches and imports static GTFS feeds. Grouping them under `ingest/` is accurate and halves the package count.
- `views.py` (SQL day views) is fine at the top level — it is already there and is small.
- `web/templatetags/` cannot be flattened: Django's template-tag loader requires the `templatetags/` directory name exactly. It stays.

## Goals / Non-Goals

**Goals:**
- Reduce total subpackage count from 6 to 2 (`ingest/`, `web/`)
- Collapse `models/` into a single flat file
- Eliminate `derive/` entirely by absorbing its files into `ingest/`
- Delete the ML subpackage and its tests completely
- All surviving tests pass with no behavior changes

**Non-Goals:**
- Renaming or reformatting any code logic (pure file moves and import fixes)
- Changing the database schema or Alembic migrations
- Touching `web/` internal structure beyond updating import paths

## Decisions

### 1. Single `models.py` instead of `models/` package

Merging all four model files into one `models.py` at the package root. The `__init__.py` re-export pattern added a layer of indirection with no benefit — every caller already imported by name.

Alternative considered: keep the `models/` package but delete `aggregates.py` (only 19 lines, just two constants). Rejected — a 4-file package is still a package; the goal is fewer layers.

### 2. New `ingest/` package absorbs `timetables/`, `gtfs/`, and `derive/`

The 10 content files (7 from `timetables/`, 3 from `gtfs/`) live flat inside `ingest/`. `finalize_tt.py` from `derive/` moves there too since it is the downstream consumer of the parsed timetable data.

`sched_sec_of` from `derive/_common.py` moves into `ingest/timeutil.py` (which already holds `epoch_to_gtfs_sec`, the one function it calls). `percentile` is deleted with the ML code.

File names inside `ingest/` are kept as-is rather than renamed — the merge is a structural move, not a rename.

### 3. Delete `derive/` entirely

With `finalize_tt.py` moved to `ingest/` and `_common.py`'s surviving function absorbed into `timeutil.py`, the `derive/` package has no remaining content.

### 4. Top-level `views.py` stays

`views.py` at the package root (SQL day views) is 70 lines and already well-placed. Moving it would only change import paths with no structural benefit.

### 5. `web/` internal structure unchanged except import paths

The web package's own layout (`queries.py`, `urls.py`, `views.py`, `wsgi.py`, `settings.py`, `templatetags/`, `static/`, `templates/`) is already flat and organized. Only import paths inside it change to reflect the new module locations.

## Risks / Trade-offs

- **Import churn** → Many files reference `dbrel.timetables.*`, `dbrel.gtfs.*`, `dbrel.models.*`, `dbrel.derive.*`. Every import path changes. Mitigation: update imports file-by-file in tasks, run tests after each subpackage migration before moving on.
- **`models/__init__.py` re-exports** → Callers currently import from `dbrel.models` (the package). Replacing with a flat `models.py` preserves the same public name, so all import statements work unchanged as long as the new file re-exports or directly defines everything that the old `__init__.py` exported.
- **Django app label** → `web/` is a Django app registered by its dotted path. If `web/` moves or is renamed, `INSTALLED_APPS` breaks. It is not moving.
- **`sched_sec_of` in `derive/_common.py`** → The docstring notes this function must never be computed two different ways. With ML deleted, the only consumer is `finalize_tt.py`, and the function moves alongside it into `ingest/`. No divergence risk.

## Migration Plan

Move and update one subpackage at a time:
1. Delete `ml/` and its tests — no dependents outside the subpackage
2. Create `ingest/` and move `timetables/` files into it; update all imports; run tests
3. Move `gtfs/` files into `ingest/`; update all imports; run tests
4. Move `derive/finalize_tt.py` into `ingest/`; move `sched_sec_of` into `ingest/timeutil.py`; delete `derive/`; run tests
5. Flatten `models/` into a single `models.py`; update all imports; run tests
6. Delete now-empty package directories and `__init__.py` stubs

No migration rollback plan needed — this is a local refactor in a single repo with git history as the safety net.
