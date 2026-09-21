"""Shared fixtures.

ml/live.py caches the active model at module level and reads it from disk on
first use. Left unmanaged, that means two failure modes: a real trained model
sitting in data/models/ leaking into a test that expects no forecast, and one
test's monkeypatch outliving it into the next. isolated_ml_cache is autouse
specifically to rule both out — every test starts from "no model" unless it
asks for one via stub_ml_model.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_ml_cache(monkeypatch):
    from dbrel.ml import live

    live.reset_cache()
    monkeypatch.setattr(live.artifacts, "latest", lambda root=None: None)
    yield
    live.reset_cache()


class StubBooster:
    """Predicts a constant and remembers how many rows it was asked about."""

    def __init__(self, value):
        self.value = value
        self.calls: list[int] = []

    def predict(self, matrix):
        self.calls.append(len(matrix))
        return [self.value] * len(matrix)


@pytest.fixture
def stub_ml_model(monkeypatch, tmp_path):
    """Activates a fake model: p10=60s, p50=120s, p90=300s, conformal +/-30s."""
    from dbrel.ml import live
    from dbrel.ml._common import FEATURE_NAMES, QUANTILES
    from dbrel.ml.artifacts import Manifest

    manifest = Manifest(
        model_version=1, model_id="20260101T000000Z", trained_at=1, active=True,
        feature_names=list(FEATURE_NAMES), quantiles=list(QUANTILES),
        conformal_adjustment=30.0,
        vocabularies={name: {} for name in ("day_type", "train_id", "station_id",
                                            "line_id", "category", "operator",
                                            "segment", "platform")},
    )
    boosters = {
        QUANTILES[0]: StubBooster(60.0),
        QUANTILES[1]: StubBooster(120.0),
        QUANTILES[2]: StubBooster(300.0),
    }
    live.reset_cache()
    monkeypatch.setattr(live.artifacts, "latest", lambda root=None: (tmp_path, manifest))
    monkeypatch.setattr(live.artifacts, "load_boosters", lambda directory, quantiles: boosters)
    yield manifest, boosters
    live.reset_cache()
