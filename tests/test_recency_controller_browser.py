"""The controlled comparison must use the reviewed Chromium binary."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_recency_browser as browser
import run_recency_training as controller


def test_controller_accepts_only_matching_complete_browser_pin(monkeypatch):
    pin = {"path": "/synthetic/chrome", "sha256": "a" * 64, "version": "Chromium fixture"}
    monkeypatch.setattr(browser, "browser_identity", lambda *_args: dict(pin))
    assert controller.validated_chromium({"chromium": pin}) == pin
    with pytest.raises(ValueError, match="Chromium"):
        controller.validated_chromium({})
    with pytest.raises(ValueError, match="Chromium"):
        controller.validated_chromium({"chromium": {"path": pin["path"]}})
    monkeypatch.setattr(browser, "browser_identity", lambda *_args: {**pin, "version": "changed"})
    with pytest.raises(ValueError, match="Chromium"):
        controller.validated_chromium({"chromium": pin})
