"""Saving and finding trained models.

On disk, not in the database. A booster is a rebuildable artifact in exactly the
sense models/aggregates.py describes for the derived tables — throw it away and
a training run makes it again — and a multi-megabyte blob in SQLite would bloat
every backup of a file that is otherwise all rows.

Boosters are written in LightGBM's own text format, never pickled. Pickle breaks
across library versions, and loading one executes whatever is inside it; a model
file is data and should stay data.

Layout, one directory per run:

    data/models/v{MODEL_VERSION}/{timestamp}Z/       an activated run
    data/models/v{MODEL_VERSION}/_rejected/{ts}Z/    one that lost to the baseline

The version directory is what makes a feature change safe: bump ML_MODEL_VERSION
when FEATURE_SPEC changes and older runs become unreachable rather than silently
mis-fed, because a booster indexes features positionally and would happily
predict from the wrong columns.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dbrel.config import DATA_DIR, ML_MODEL_VERSION
from dbrel.ml._common import FEATURE_NAMES

MANIFEST = "manifest.json"
REJECTED = "_rejected"


@dataclass(slots=True)
class Manifest:
    """Everything needed to reproduce a prediction, and to refuse a stale model."""

    model_version: int
    model_id: str
    trained_at: int
    active: bool
    feature_names: list[str]
    quantiles: list[float]
    conformal_adjustment: float
    vocabularies: dict[str, dict[str, int]]
    fit_dates: list[str] = field(default_factory=list)
    calibrate_dates: list[str] = field(default_factory=list)
    test_dates: list[str] = field(default_factory=list)
    n_fit: int = 0
    n_calibrate: int = 0
    n_test: int = 0
    metrics: dict = field(default_factory=dict)
    rejected_reason: str | None = None


def models_root() -> Path:
    return Path(DATA_DIR) / "models" / f"v{ML_MODEL_VERSION}"


def new_model_id(now: int | None = None) -> str:
    stamp = datetime.fromtimestamp(now, timezone.utc) if now else datetime.now(timezone.utc)
    return stamp.strftime("%Y%m%dT%H%M%SZ")


def save(
    boosters: dict[float, object],
    manifest: Manifest,
    *,
    root: Path | None = None,
) -> Path:
    """Write the boosters and their manifest. Rejected runs go somewhere unloadable."""
    base = root or models_root()
    directory = (base / manifest.model_id) if manifest.active else (base / REJECTED / manifest.model_id)
    directory.mkdir(parents=True, exist_ok=True)
    for quantile, booster in boosters.items():
        booster.save_model(str(directory / f"q{int(quantile * 100)}.txt"))
    (directory / MANIFEST).write_text(json.dumps(asdict(manifest), indent=2))
    return directory


def load_manifest(directory: Path) -> Manifest:
    data = json.loads((directory / MANIFEST).read_text())
    return Manifest(**data)


def latest(root: Path | None = None) -> tuple[Path, Manifest] | None:
    """The newest activated model for the current feature version, or None.

    Only looks inside v{ML_MODEL_VERSION}, so a model trained against a different
    FEATURE_SPEC is not merely deprioritised — it is invisible.
    """
    base = root or models_root()
    if not base.is_dir():
        return None
    candidates = []
    for directory in base.iterdir():
        if not directory.is_dir() or directory.name == REJECTED:
            continue
        if not (directory / MANIFEST).is_file():
            continue
        manifest = load_manifest(directory)
        if manifest.active and manifest.feature_names == list(FEATURE_NAMES):
            candidates.append((directory.name, directory, manifest))
    if not candidates:
        return None
    _, directory, manifest = max(candidates, key=lambda c: c[0])
    return directory, manifest


def load_boosters(directory: Path, quantiles) -> dict[float, object]:
    import lightgbm as lgb

    return {
        q: lgb.Booster(model_file=str(directory / f"q{int(q * 100)}.txt"))
        for q in quantiles
    }
