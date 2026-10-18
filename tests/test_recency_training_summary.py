"""Small CPU-only tests for the report-only recency summary primitives."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import summarize_recency_training as summary  # noqa: E402

CHROMIUM = {"path": "/fixture/chromium", "sha256": "a" * 64, "version": "130.0.6723.0"}
RUNTIME_VERSION = "1.30.0"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _pin_browser(root):
    readiness = root / "data/ml/recency_training_20261013/readiness.json"
    readiness.parent.mkdir(parents=True, exist_ok=True)
    readiness.write_text(json.dumps({"chromium": dict(CHROMIUM)}))
    return dict(CHROMIUM)


def test_shown_matches_nonnegative_clamp_and_positive_round_half_up():
    points = np.array([-2.0, 0.99, 1.0, 1.49, 1.5, 2.5], dtype=np.float32)
    assert np.array_equal(summary.shown(points), [0, 0, 1, 1, 2, 3])


def test_score_reports_all_eligible_denominators_and_missing_predictions():
    score = summary.Score()
    score.add(np.array([0, 4, 8]), np.array([0, np.nan, 4]))
    result = score.result()
    assert result == {
        "eligible": 3,
        "scored": 2,
        "missing": 1,
        "coverage": 2 / 3,
        "mae_scored": 2.0,
        "within_1_min": 1 / 3,
        "within_3_min": 1 / 3,
        "within_5_min": 2 / 3,
    }


def test_paired_day_bootstrap_is_deterministic_and_directional():
    def packed(y, p):
        score = summary.Score()
        score.add(np.asarray(y), np.asarray(p))
        return score.sufficient()

    control = {"raw": {"d1": packed([0, 10], [0, 0]), "d2": packed([0], [10])},
               "displayed": {"d1": packed([0, 10], [0, 0]), "d2": packed([0], [10])}}
    weighted = {"raw": {"d1": packed([0, 10], [0, 9]), "d2": packed([0], [0])},
                "displayed": {"d1": packed([0, 10], [0, 9]), "d2": packed([0], [0])}}
    a = summary.paired_day_bootstrap(control, weighted, seed=4231, replicates=100)
    b = summary.paired_day_bootstrap(control, weighted, seed=4231, replicates=100)
    assert a == b
    assert a["weighted_minus_control"]["raw"]["within_5_min"]["weighted_minus_control"] > 0
    assert a["weighted_minus_control"]["raw"]["mae_scored"]["weighted_minus_control"] < 0


def test_completed_run_verification_binds_producer_ids_arrays_history_and_browser(tmp_path, monkeypatch):
    root = tmp_path
    monkeypatch.setattr(summary, "ROOT", root)
    monkeypatch.setattr(summary, "PREPARED", root / "data/prepared/eval")
    pinned_chromium = _pin_browser(root)
    run = root / "run"
    run.mkdir()
    points = np.array([1.0, 2.0, 3.0], dtype="<f4")
    ids = np.array([11, 12, 13], dtype="<i8")
    np.save(run / "median.npy", points)
    np.save(run / "stop_ids.npy", ids)
    _write(run / "history.json", json.dumps({"epochs": [{"epoch": 1}]}).encode())
    prepared = SimpleNamespace(key={"cache": "fixture"})
    key = {"condition": "control", "mode": "train", "prepared_root": "data/prepared",
           "validation": prepared.key}
    (run / "key.json").write_text(json.dumps(key))
    artifact = root / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"fake-onnx")
    manifest_path = artifact / "manifest.json"
    manifest = {"model_file": "model.onnx", "role": "comparison",
                "asset_bytes": (artifact / "model.onnx").stat().st_size}
    for _ in range(10):
        manifest_path.write_text(json.dumps(manifest))
        measured = (artifact / "model.onnx").stat().st_size + manifest_path.stat().st_size
        if manifest.get("download_bytes") == measured:
            break
        manifest["download_bytes"] = measured
    manifest_path.write_text(json.dumps(manifest))
    measured = (artifact / "model.onnx").stat().st_size + manifest_path.stat().st_size
    assert manifest["download_bytes"] == measured
    browser = {"passed": True, "chromium": pinned_chromium, "runtime_version": RUNTIME_VERSION,
               "cases": {"median": {"max_abs_vs_python": 0.0,
                                      "single_vs_batch_max_abs": 0.0, "wrong_shape": 0,
                                      "single_unknown_categories_checked": True, "rows": 2}}}
    (run / "actual_frontend_browser.json").write_text(json.dumps(browser))
    result = {"track": "fine", "history_sha256": hashlib.sha256((run / "history.json").read_bytes()).hexdigest(),
              "prediction_identity": {"stop_ids_sha256": hashlib.sha256((run / "stop_ids.npy").read_bytes()).hexdigest(),
                                      "arrays": {"median.npy": hashlib.sha256((run / "median.npy").read_bytes()).hexdigest()}},
              "later_probe": {"median": {"overall": {"eligible": 3, "quantile_scored": 3,
                                                         "band_coverage": 0.5, "below_p25": 0.25,
                                                         "below_p75": 0.75}}},
              "onnx": {"onnx_passed": True}, "within_experimental_budget": True,
              "artifact": {"path": "artifact", "download_bytes": measured}, "resources": {"cgroup_peak_bytes": 123}}
    (run / "result.json").write_text(json.dumps(result))
    verified = summary._verify_run(run, "control", "fine", prepared, 3, validate_provenance=False)
    assert verified["points"].tolist() == points.tolist()
    assert verified["export"]["browser_passed"]
    result["history_sha256"] = "bad"
    (run / "result.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="history hash"):
        summary._verify_run(run, "control", "fine", prepared, 3, validate_provenance=False)


