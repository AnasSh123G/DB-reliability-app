"""Small CPU-only tests for the report-only recency summary primitives."""
from __future__ import annotations

import hashlib
import json
import sqlite3
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


def test_later_probe_requires_full_quantile_denominators_and_finite_rates():
    good = {"later_probe": {"median": {"overall": {
        "eligible": 4, "quantile_scored": 4, "band_coverage": 0.75,
        "below_p25": 0.25, "below_p75": 0.75}}}}
    assert summary._validated_later_probe(good, "median", 4, "fixture") == {
        "band_coverage": 0.75, "below_p25": 0.25, "below_p75": 0.75}
    for field, value in (("quantile_scored", 3), ("band_coverage", None),
                         ("below_p25", float("nan")), ("below_p75", 1.01)):
        broken = json.loads(json.dumps(good))
        broken["later_probe"]["median"]["overall"][field] = value
        with pytest.raises(ValueError, match="quantile"):
            summary._validated_later_probe(broken, "median", 4, "fixture")
    with pytest.raises(ValueError, match="quantile-scored"):
        summary._validated_later_probe({"later_probe": {"median": {"overall": {"eligible": 4}}}},
                                       "median", 4, "fixture")


def test_august_result_is_mandatory_and_bound_to_current_script(tmp_path, monkeypatch):
    monkeypatch.setattr(summary, "ROOT", tmp_path)
    monkeypatch.setattr(summary, "EXPERIMENT", tmp_path / "data/ml/recency_training_20261013")
    with pytest.raises(ValueError, match="mandatory corrected August"):
        summary._verify_corrected_august(tmp_path, {})
    base = tmp_path / "data/ml/recency_training_20261013/corrected_august_comparison"
    base.mkdir(parents=True)
    script = tmp_path / "scripts/compare_corrected_august.py"
    _write(script, b"synthetic producer")
    (base / "result.json").write_text(json.dumps({"script_sha": "0" * 64}))
    with pytest.raises(ValueError, match="script identity"):
        summary._verify_corrected_august(tmp_path, {})


def test_frozen_readonly_source_database_is_hash_wal_and_snapshot_guarded(tmp_path):
    db = tmp_path / "data/dbrel.sqlite"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO sample VALUES (1)")
    conn.commit()
    conn.close()
    source_inputs = tmp_path / "data/ml/trip_schedule_correction_20261013/source-inputs.json"
    _write(source_inputs, json.dumps({"database": {"sha256": summary.sha256_file(db)}}).encode())
    with summary._frozen_source_database(tmp_path, db) as readonly:
        assert readonly.execute("PRAGMA query_only").fetchone()[0] == 0
        assert readonly.execute("PRAGMA cache_size").fetchone()[0] == -32768
        assert readonly.execute("PRAGMA temp_store").fetchone()[0] == 1
        assert readonly.execute("SELECT id FROM sample").fetchall() == [(1,)]

    wal = Path(str(db) + "-wal")
    wal.write_bytes(b"uncheckpointed fixture")
    with pytest.raises(ValueError, match="nonempty WAL"):
        with summary._frozen_source_database(tmp_path, db):
            pytest.fail("WAL must be rejected before reads")
    wal.unlink()

    with pytest.raises(ValueError, match="SHA"):
        _write(source_inputs, json.dumps({"database": {"sha256": "f" * 64}}).encode())
        with summary._frozen_source_database(tmp_path, db):
            pytest.fail("changed provenance digest must be rejected")

    _write(source_inputs, json.dumps({"database": {"sha256": summary.sha256_file(db)}}).encode())
    with pytest.raises(ValueError, match="changed after metadata reads"):
        with summary._frozen_source_database(tmp_path, db):
            db.touch()


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


def test_trip_date_accumulator_merges_small_blocks_on_disk(tmp_path):
    accumulator = summary.TripDateAccumulator(tmp_path / "trip-date.sqlite")
    target = np.array([0.0, 10.0, 0.0])
    control = {"fine": np.array([0.0, 0.0, 10.0]),
               "window3": np.array([0.0, 0.0, 10.0]),
               "discrete": np.array([0.0, 0.0, 10.0])}
    weighted = {name: np.array([0.0, 9.0, 0.0]) for name in control}
    accumulator.add(np.array(["t1", "t1", "t2"]), target,
                    {"control": control, "weighted": weighted})
    accumulator.add(np.array(["t2"]), np.array([10.0]),
                    {"control": {name: np.array([0.0]) for name in control},
                     "weighted": {name: np.array([9.0]) for name in control}})
    result = accumulator.result()
    accumulator.close()
    assert result["fine"]["trip_date_clusters"] == 2
    assert result["fine"]["metrics"]["within_5_min_displayed"]["weighted_minus_control_mean"] > 0
    assert result["fine"]["metrics"]["mae_displayed"]["weighted_minus_control_mean"] < 0


def test_ordered_id_check_accepts_exact_ids_and_rejects_reordering():
    expected = np.array([11, 12, 13], dtype="<i8")
    summary._validate_id_sequence(expected.copy(), expected, "fixture", block_rows=2)
    reordered = expected[[1, 0, 2]]
    with pytest.raises(ValueError, match="ordered stop IDs differ"):
        summary._validate_id_sequence(reordered, expected, "fixture", block_rows=2)


def test_correction_status_manifest_and_per_date_proof_are_verified(tmp_path, monkeypatch):
    root = tmp_path
    correction = root / "correction"
    prepared_dir = correction / "prepared" / "eval"
    original_dir = root / "original" / "eval"
    monkeypatch.setattr(summary, "ROOT", root)
    monkeypatch.setattr(summary, "ORIGINAL_PREPARED", original_dir)
    dates = np.array([int(np.datetime64("2026-09-06", "D").astype(int))] * 2 +
                     [int(np.datetime64("2026-09-07", "D").astype(int))], dtype="<i4")
    targets = np.array([4, 5, 6], dtype="<f4")
    status = np.array([1, 2, 5], dtype="u1")
    (prepared_dir / "day.bin").parent.mkdir(parents=True, exist_ok=True)
    _write(prepared_dir / "day.bin", dates.tobytes())
    corrected_y_sha = _write(prepared_dir / "y.bin", targets.tobytes())
    status_sha = _write(prepared_dir / summary.STATUS_NAME, status.tobytes())
    original_y_sha = _write(original_dir / "y.bin", np.array([7, 5, 6], dtype="<f4").tobytes())
    record = {"status": "complete", "columns": {
        "y.bin": {"bytes": (prepared_dir / "y.bin").stat().st_size, "sha256": corrected_y_sha},
    }}
    day0 = str(np.datetime64("2026-09-06", "D"))
    day1 = str(np.datetime64("2026-09-07", "D"))
    progress = {
        day0: {"role": "eval", "blocks_sha256": {"status": hashlib.sha256(status[:2].tobytes()).hexdigest()}},
        day1: {"role": "eval", "blocks_sha256": {"status": hashlib.sha256(status[2:].tobytes()).hexdigest()}},
    }
    (correction / "progress.json").write_text(json.dumps(progress))
    record["progress_sha"] = hashlib.sha256((correction / "progress.json").read_bytes()).hexdigest()
    (correction / "result.json").write_text(json.dumps({"prepared_root": "correction/prepared"}))
    (correction / "source-inputs.json").write_text(json.dumps({"columns": {"eval": {
        "y.bin": {"sha256": original_y_sha}}}}))

    class Prepared:
        dir = prepared_dir
        key = {"label_correction": record}
        meta = {"label_correction": record}

        def __len__(self):
            return 3

        def column(self, name):
            return {"day": dates}[name]

    _, counts, dates_seen, identity = summary._correction_status(Prepared(), correction)
    assert counts == {"0": 0, "1": 1, "2": 1, "3": 0, "4": 0, "5": 1}
    assert dates_seen == [day0, day1]
    assert identity["status_sha256"] == status_sha

    progress[day0]["blocks_sha256"]["status"] = "0" * 64
    (correction / "progress.json").write_text(json.dumps(progress))
    record["progress_sha"] = hashlib.sha256((correction / "progress.json").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="status proof"):
        summary._correction_status(Prepared(), correction)


def test_bounded_sqlite_id_lookup_checks_service_date_and_preserves_order():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE trip_stop(id INTEGER PRIMARY KEY, trip_id INTEGER, station_id INTEGER);
      CREATE TABLE trip(id INTEGER PRIMARY KEY, train_id INTEGER, service_date TEXT, source TEXT);
      CREATE TABLE train(id INTEGER PRIMARY KEY, route_id INTEGER, segment TEXT);
      CREATE TABLE station(id INTEGER PRIMARY KEY, state TEXT);
    """)
    conn.executemany("INSERT INTO station VALUES (?,?)", [(1, "DE-BY"), (2, None)])
    conn.executemany("INSERT INTO train VALUES (?,?,?)", [(1, 9, "RV"), (2, None, "S")])
    conn.executemany("INSERT INTO trip VALUES (?,?,?,?)", [
        (1, 1, "2026-09-06", "tts"), (2, 2, "2026-09-06", "gtfs"),
        (3, 1, "2026-09-07", "tts")])
    conn.executemany("INSERT INTO trip_stop VALUES (?,?,?)", [(101, 1, 1), (102, 2, 2), (103, 3, 1)])
    day_values = np.array([int(np.datetime64(d, "D").astype(int)) for d in
                           ("2026-09-06", "2026-09-06", "2026-09-07")], dtype="<i4")
    segments = np.array([1, 2, 1], dtype="<i2")
    dictionary = ["", "RV", "S"]
    batches = list(summary._dimensions_from_lookup(conn, np.array([101, 102, 103]), day_values,
                                                   segments, dictionary, block_rows=2, sql_limit=1))
    assert [item[2]["state"].tolist() for item in batches] == [["DE-BY", "UNKNOWN"], ["DE-BY"]]
    assert [item[2]["route"].tolist() for item in batches] == [["9", "UNKNOWN"], ["9"]]
    assert [item[2]["service"].tolist() for item in batches] == [["Regional", "S-Bahn"], ["Regional"]]
    conn.close()

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE trip_stop(id INTEGER PRIMARY KEY, trip_id INTEGER, station_id INTEGER);
      CREATE TABLE trip(id INTEGER PRIMARY KEY, train_id INTEGER, service_date TEXT, source TEXT);
      CREATE TABLE train(id INTEGER PRIMARY KEY, route_id INTEGER, segment TEXT);
      CREATE TABLE station(id INTEGER PRIMARY KEY, state TEXT);
      INSERT INTO station VALUES (1,'DE-BY'); INSERT INTO train VALUES (1,9,'RV');
      INSERT INTO trip VALUES (1,1,'2026-09-07','tts'); INSERT INTO trip_stop VALUES (101,1,1);
    """)
    with pytest.raises(ValueError, match="service_date"):
        list(summary._dimensions_from_lookup(conn, np.array([101]), day_values[:1],
                                             np.array([1], dtype="<i2"), dictionary))


def test_producer_schema_preflight_and_full_accumulation_are_bounded_and_aligned(tmp_path):
    database = tmp_path / "readonly-source.sqlite"
    conn = sqlite3.connect(database)
    conn.executescript("""
      CREATE TABLE trip_stop(id INTEGER PRIMARY KEY, trip_id INTEGER, station_id INTEGER);
      CREATE TABLE trip(id INTEGER PRIMARY KEY, train_id INTEGER, service_date TEXT, source TEXT);
      CREATE TABLE train(id INTEGER PRIMARY KEY, route_id INTEGER, segment TEXT);
      CREATE TABLE station(id INTEGER PRIMARY KEY, state TEXT);
      INSERT INTO station VALUES (1,'BY');
      INSERT INTO train VALUES (1,9,'RV'); INSERT INTO train VALUES (2,NULL,'FV');
      INSERT INTO trip VALUES (1,1,'2026-09-06','tts');
      INSERT INTO trip VALUES (2,2,'2026-09-07','gtfs');
      INSERT INTO trip_stop VALUES (101,1,1); INSERT INTO trip_stop VALUES (102,1,1);
      INSERT INTO trip_stop VALUES (103,2,1);
    """)
    conn.commit()
    conn.close()
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    days = np.array([int(np.datetime64(day, "D").astype(int)) for day in
                     ("2026-09-06", "2026-09-06", "2026-09-07")], dtype="<i4")
    columns = {"day": days, "stop_id": np.array([101, 102, 103], dtype="<i8"),
               "segment": np.array([1, 1, 2], dtype="<i2")}

    class Prepared:
        y = np.array([0.0, 10.0, 5.0], dtype="<f4")

        def column(self, name):
            return columns[name]

    prepared = Prepared()
    dictionary = ["", "RV", "S"]
    preflight = summary._preflight_group_cardinality(conn, prepared, 0, 3, dictionary)
    assert preflight["unknown_route_rows"] == 1
    assert preflight["unknown_route_rows_by_source"] == {"gtfs": 1}
    assert preflight["unique_labels_by_dimension"]["route"] == 2
    assert preflight["prepared_database_segment_mismatch_rows"] == 1
    candidate_sets = {}
    for condition, shift in (("control", 0.0), ("weighted", -1.0)):
        candidate_sets[condition] = {
            name: {"points": np.array([0.0, 9.0, 5.0], dtype="<f4") + shift}
            for name in ("fine", "window3", "discrete")}
    scores, day_sums, counts, status_counts, clusters = summary._accumulate(
        candidate_sets, prepared, np.array([1, 5, 2], dtype="u1"), 0, 3, 0,
        conn, tmp_path / "trip-clusters.sqlite", dictionary)
    overall = scores["control"]["raw"]["fine"]["complete"]["overall"]["all"].result()
    assert overall["eligible"] == 3 and overall["within_1_min"] == 1.0
    assert len(day_sums["weighted"]["window3"]["displayed"]) == 2
    assert counts == {"2026-09-06": 1, "2026-09-07": 1}
    assert status_counts == {"0": 0, "1": 1, "2": 1, "3": 0, "4": 0, "5": 1}
    assert clusters["fine"]["trip_date_clusters"] == 2
    conn.close()


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


def test_correction_baseline_verifier_recomputes_same_cohort_effects(tmp_path):
    root = tmp_path
    correction = root / "data/ml/trip_schedule_correction_20261013"
    baseline = root / "data/ml/recency_training_20261013/correction_baselines"
    baseline.mkdir(parents=True)
    source_dir = root / "data/ml/full_year/prepared" / f"{summary.PREFIX}eval"
    source_dir.mkdir(parents=True)
    old_y = np.array([10, 5, 7, 0], dtype="<f4")
    old_sha = _write(source_dir / "y.bin", old_y.tobytes())
    (correction / "source-inputs.json").parent.mkdir(parents=True, exist_ok=True)
    (correction / "source-inputs.json").write_text(json.dumps({"columns": {"eval": {
        "y.bin": {"sha256": old_sha}}}}))
    reference = root / "data/ml/full_year/runs/compact-2026-09-26/trials/f1ac12b1742e3a0b/validation.npz"
    _write(reference, b"reference fixture")
    baseline_script = root / "scripts/score_label_correction_baselines.py"
    _write(baseline_script, b"score script fixture")
    graph_records = {}
    for name in ("fine", "distribution", "discrete"):
        graph_dir = root / "data/ml/compact_training_20261013/runs" / name / "onnx"
        _write(graph_dir / "model.onnx", name.encode())
        _write(graph_dir / "prep.json", (name + "prep").encode())
        graph_records[name] = {"onnx_sha": summary.sha256_file(graph_dir / "model.onnx"),
                               "prep_sha": summary.sha256_file(graph_dir / "prep.json")}
    status = np.array([0, 1, 5, 2], dtype="u1")
    targets = np.array([10, 4, 7, 2], dtype="<f4")
    points = {name: np.array([4, 6, 1], dtype="<f4") + offset
              for name, offset in (("reference", 0), ("fine", 0),
                                   ("distribution", 1), ("discrete", 2))}
    arrays = {}
    for name, values in points.items():
        array_path = baseline / f"{name}.npy"
        np.save(array_path, values)
        arrays[array_path.name] = summary.sha256_file(array_path)
    ids = np.array([102, 103, 104], dtype="<i8")
    ids_path = baseline / "stop_ids.npy"
    np.save(ids_path, ids)
    arrays[ids_path.name] = summary.sha256_file(ids_path)
    prepared_key = {"prepared": "fixture"}
    status_path = correction / "prepared" / f"{summary.PREFIX}eval" / summary.STATUS_NAME
    _write(status_path, status.tobytes())
    key = {"script_sha": summary.sha256_file(baseline_script),
           "correction_result_sha": "c" * 64, "prepared_key": prepared_key,
           "reference_sha": summary.sha256_file(reference), "status_sha": summary.sha256_file(status_path),
           "graphs": graph_records}
    key_path = baseline / "key.json"
    key_path.write_text(json.dumps(key))
    labels = ("original", "corrected", "corrected_verified", "corrected_unresolved")
    score_map = {}
    for name, prediction in points.items():
        score_map[name] = {}
        for label, target, mask in (
                ("original", old_y[1:], np.ones(3, dtype=bool)),
                ("corrected", targets[1:], np.ones(3, dtype=bool)),
                ("corrected_verified", targets[1:], np.isin(status[1:], (1, 2))),
                ("corrected_unresolved", targets[1:], ~np.isin(status[1:], (1, 2)))):
            score_map[name][label] = {}
            for view, values in (("raw", prediction), ("displayed", summary.shown(prediction))):
                score = summary.Score()
                score.add(target[mask], values[mask])
                score_map[name][label][view] = score.result()
    (baseline / "result.json").write_text(json.dumps({"key_sha": summary.sha256_file(key_path),
        "rows": 3, "scores": score_map, "arrays": arrays,
        "scope": "synthetic unchanged export rescore"}))

    class Prepared:
        key = prepared_key
        y = targets

        def __len__(self):
            return 4

        def column(self, name):
            return {"stop_id": np.array([101, 102, 103, 104], dtype="<i8")}[name]

    identity = {"correction_result_sha256": "c" * 64,
                "status_sha256": summary.sha256_file(status_path)}
    checked = summary._verify_correction_baseline(root, Prepared(), status, 1, 3, 1, identity)
    assert checked["cohort_rows"] == 2
    assert checked["effects"]["reference"]["raw"]["correction_minus_original"]["mae_scored"] < 0


def test_discrete_export_requires_distribution_history_sha_from_completed_result(tmp_path, monkeypatch):
    experiment = tmp_path / "experiment"
    monkeypatch.setattr(summary, "EXPERIMENT", experiment)
    source = experiment / "control/runs/distribution"
    discrete = experiment / "control/runs/discrete"
    checkpoint = _write(source / "epoch-003.pt", b"immutable epoch weights")
    (source / "history.json").write_text(json.dumps({"epochs": [
        {"epoch": 3, "checkpoint_sha256": checkpoint}]}))
    source_key = {"condition": "control", "mode": "train", "fit": {"same": True}}
    source_result = {"history_sha256": summary.sha256_file(source / "history.json")}
    export_file = _write(tmp_path / "export/model.onnx", b"export graph")
    discrete_script = _write(tmp_path / "scripts/check_recency_discrete_delay.py", b"discrete script")
    provenance_script = _write(tmp_path / "scripts/recency_provenance.py", b"provenance script")
    key = {"source_key": source_key, "selected_epoch": 3, "source_weights_sha": checkpoint,
           "script_sha": discrete_script, "provenance_sha": provenance_script}
    result = {"source_weights_sha": checkpoint, "selected_epoch": 3,
              "export_identity": {"export/model.onnx": export_file}}
    (discrete / "key.json").parent.mkdir(parents=True, exist_ok=True)
    (discrete / "key.json").write_text(json.dumps(key))
    (discrete / "result.json").write_text(json.dumps(result))
    summary._verify_discrete_source(discrete, "control",
                                    {"key": source_key, "result": source_result}, tmp_path)
    source_result["history_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="history differs"):
        summary._verify_discrete_source(discrete, "control",
                                        {"key": source_key, "result": source_result}, tmp_path)


def test_export_gate_requires_actual_browser_pass_and_manifest_inclusive_size(tmp_path):
    run = tmp_path / "run"
    (run / "browser_check").mkdir(parents=True)
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"onnx-fixture")
    manifest = {"model_file": "model.onnx", "role": "comparison",
                "asset_bytes": (artifact / "model.onnx").stat().st_size}
    manifest_path = artifact / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=1))
    # The declared size includes the manifest itself; iterate to a stable width.
    for _ in range(10):
        measured = (artifact / "model.onnx").stat().st_size + manifest_path.stat().st_size
        if manifest.get("download_bytes") == measured:
            break
        manifest["download_bytes"] = measured
        manifest_path.write_text(json.dumps(manifest, indent=1))
    measured = (artifact / "model.onnx").stat().st_size + manifest_path.stat().st_size
    assert manifest["download_bytes"] == measured
    pinned_chromium = _pin_browser(tmp_path)
    browser = {"passed": True, "chromium": pinned_chromium,
               "runtime_version": RUNTIME_VERSION,
               "cases": {"median": {"max_abs_vs_python": 0.0,
                                      "single_vs_batch_max_abs": 0.0, "wrong_shape": 0,
                                      "single_unknown_categories_checked": True, "rows": 4}}}
    (run / "actual_frontend_browser.json").write_text(json.dumps(browser))
    result = {"onnx": {"onnx_passed": True}, "within_experimental_budget": True,
              "artifact": {"path": str(artifact.relative_to(tmp_path)), "download_bytes": measured,
                           "fits_strict_35mb": True}}
    gate = summary._check_export_and_browser(run, result, "fine", tmp_path)
    assert gate["browser_passed"] and gate["manifest_inclusive_download_bytes"] == measured

    browser.pop("chromium")
    (run / "actual_frontend_browser.json").write_text(json.dumps(browser))
    with pytest.raises(ValueError, match="Chromium identity"):
        summary._check_export_and_browser(run, result, "fine", tmp_path)

    browser["chromium"] = {**pinned_chromium, "sha256": "b" * 64}
    (run / "actual_frontend_browser.json").write_text(json.dumps(browser))
    with pytest.raises(ValueError, match="Chromium identity"):
        summary._check_export_and_browser(run, result, "fine", tmp_path)

    browser["chromium"] = pinned_chromium
    stale_pin = {**pinned_chromium, "version": "Chromium stale fixture"}
    (tmp_path / "data/ml/recency_training_20261013/readiness.json").write_text(
        json.dumps({"chromium": stale_pin}))
    (run / "actual_frontend_browser.json").write_text(json.dumps(browser))
    with pytest.raises(ValueError, match="Chromium identity"):
        summary._check_export_and_browser(run, result, "fine", tmp_path)

    _pin_browser(tmp_path)
    browser["runtime_version"] = "1.29.0"
    (run / "actual_frontend_browser.json").write_text(json.dumps(browser))
    with pytest.raises(ValueError, match="1.30.0"):
        summary._check_export_and_browser(run, result, "fine", tmp_path)

    browser["runtime_version"] = RUNTIME_VERSION
    browser["passed"] = False
    (run / "actual_frontend_browser.json").write_text(json.dumps(browser))
    with pytest.raises(ValueError, match="browser runtime parity"):
        summary._check_export_and_browser(run, result, "fine", tmp_path)
