"""Shared fixtures."""

import sys

import pytest


@pytest.fixture(autouse=True)
def _no_deployed_model(tmp_path_factory, monkeypatch):
    """Keep the web layer off the real `models/v2`.

    The pages look up the deployed model through `ML_MODELS_DIR`; without this, every page test
    would run whatever model is CURRENT on this machine against its small fixture database (a model
    with notice-rate inputs needs table rows a fixture lacks). Tests that want a model point the
    modules at their own artifact, which overrides this default.
    """
    empty = tmp_path_factory.mktemp("no-models")
    for name in ("dbrel.web.queries", "dbrel.web.views"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "ML_MODELS_DIR"):
            monkeypatch.setattr(module, "ML_MODELS_DIR", empty)
    queries = sys.modules.get("dbrel.web.queries")
    if queries is not None:
        monkeypatch.setattr(queries, "_model_cache", {"stamp": None, "manifest": None})
