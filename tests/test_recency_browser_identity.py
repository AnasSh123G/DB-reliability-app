import hashlib
import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_recency_browser as checker


def _stub_browser(path: Path, version: str) -> str:
    path.write_text(f"#!/bin/sh\necho '{version}'\n")
    path.chmod(0o755)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_browser_identity_records_verified_binary_and_version(tmp_path):
    binary = tmp_path / "chrome-stub"
    digest = _stub_browser(binary, "Chromium 130.0.1")

    identity = checker.browser_identity(binary, digest)

    assert identity == {"path": str(binary.resolve()), "sha256": digest, "version": "Chromium 130.0.1"}


def test_browser_identity_rejects_hash_mismatch(tmp_path):
    binary = tmp_path / "chrome-stub"
    _stub_browser(binary, "Chromium 130.0.1")

    with pytest.raises(ValueError, match="sha256"):
        checker.browser_identity(binary, "0" * 64)


def test_browser_identity_rejects_missing_version(tmp_path):
    binary = tmp_path / "chrome-stub"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="version"):
        checker.browser_identity(binary, digest)


def test_output_identity_requires_all_pinned_fields():
    pin = "a" * 64
    assert not checker._valid_output_identity({}, pin)
    assert not checker._valid_output_identity({"chromium": {
        "path": "/chrome", "sha256": "b" * 64, "version": "Chromium"}}, pin)
    assert not checker._valid_output_identity({"chromium": {
        "path": "/chrome", "sha256": pin}}, pin)
