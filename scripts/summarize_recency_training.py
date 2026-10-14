"""Summarize completed recency runs on the corrected September 6–19 cohort.

This is a report-only consumer. It verifies completed training provenance,
prediction hashes and ordered IDs, correction-status hashes, browser parity and
manifest-inclusive artifact sizes before scoring the saved point arrays. It
does not load a model, run inference, change CURRENT, or launch training.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import csv
import tempfile
from contextlib import contextmanager
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dbrel.ml.full_year import PreparedSet
from recency_provenance import PREFIX, file_hash, verify_training_key
from recency_experiment import (CONDITIONS, DEFAULT_CONDITIONS, NO_DOY_TRIAL, DEFAULT_RELATIVE,
                                conditions as experiment_conditions, directory as experiment_directory)
from browser_parity import report_passes

EXPERIMENT = experiment_directory(ROOT)
CORRECTION = ROOT / "data/ml/trip_schedule_correction_20261013"
PREPARED = CORRECTION / "prepared" / f"{PREFIX}eval"
ORIGINAL_PREPARED = ROOT / "data/ml/full_year/prepared" / f"{PREFIX}eval"
SUMMARY_DIR = EXPERIMENT / "summary"
REPORT = ROOT / "docs/recency-training-results.md"
STATUS_NAME = "label_status.u8"
BLOCK_ROWS = 131072
SQL_IDS = 400
START_DAY = "2026-09-06"
END_DAY = "2026-09-20"  # exclusive
DAY_BOOTSTRAP_SEED = 4231
DAY_BOOTSTRAP_REPLICATES = 2000
MODEL_FILES = {"fine": "median.npy", "window3": "window3.npy", "discrete": "median.npy"}
SUBSETS = ("complete", "verified_1_2", "unresolved_0_3_4_5")
METRICS = ("within_1_min", "within_3_min", "within_5_min", "mae_scored")


def shown(point):
    """Match the app: estimates below one display as 0; positive ties round up."""
    point = np.asarray(point, dtype=np.float64)
    return np.where(point < 1, 0.0, np.floor(point + 0.5))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(stream.fileno(), stream.tell() - len(chunk), len(chunk), os.POSIX_FADV_DONTNEED)
    return digest.hexdigest()


def _database_stamp(path: Path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _assert_database_snapshot(path: Path, expected_stamp, phase: str):
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError(f"source database has a nonempty WAL {phase}")
    if _database_stamp(path) != expected_stamp:
        raise ValueError(f"source database size or mtime changed {phase}")


def _validated_later_probe(result, probe_name: str, expected_rows: int, context: str):
    probe = result.get("later_probe", {}).get(probe_name)
    overall = probe.get("overall") if isinstance(probe, dict) else None
    fields = ("band_coverage", "below_p25", "below_p75")
    if not isinstance(overall, dict) or overall.get("eligible") != expected_rows:
        raise ValueError(f"{context}: saved later-comparison metric row count differs")
    if overall.get("quantile_scored") != expected_rows:
        raise ValueError(f"{context}: saved quantile-scored row count differs")
    values = {}
    for field in fields:
        value = overall.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not np.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{context}: missing or invalid saved quantile diagnostic {field}")
        values[field] = float(value)
    return values


@contextmanager
def _frozen_source_database(root: Path, database_path: Path):
    """Open source metadata under a content-bound, read-only SQLite snapshot."""
    root, database_path = Path(root).resolve(), Path(database_path).resolve()
    inputs_path = root / "data/ml/trip_schedule_correction_20261013/source-inputs.json"
    source_inputs = _read_json(inputs_path)
    expected_sha = source_inputs.get("database", {}).get("sha256")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise ValueError("correction source-inputs lacks a database SHA-256")
    stamp = _database_stamp(database_path)
    _assert_database_snapshot(database_path, stamp, "before hashing")
    if sha256_file(database_path) != expected_sha:
        raise ValueError("source database SHA differs from correction source-inputs")
    _assert_database_snapshot(database_path, stamp, "after hashing")
    uri = f"file:{database_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA cache_size=-32768")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("BEGIN")
        version_before = conn.execute("PRAGMA data_version").fetchone()[0]
        _assert_database_snapshot(database_path, stamp, "after BEGIN")
        yield conn
        _assert_database_snapshot(database_path, stamp, "after metadata reads")
        conn.rollback()
        version_after = conn.execute("PRAGMA data_version").fetchone()[0]
        if version_after != version_before:
            raise ValueError("source database data_version changed during metadata reads")
        _assert_database_snapshot(database_path, stamp, "after rollback")
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


class Score:
    """Mergeable sufficient statistics for full-population arrival metrics."""

    def __init__(self):
        self.eligible = self.scored = 0
        self.error_sum = 0.0
        self.hits = {1: 0, 3: 0, 5: 0}

    def add(self, target, point):
        target = np.asarray(target, dtype=np.float64)
        point = np.asarray(point, dtype=np.float64)
        if target.ndim != 1 or point.shape != target.shape:
            raise ValueError("target and point arrays must be matching one-dimensional arrays")
        if not np.isfinite(target).all() or np.isinf(point).any():
            raise ValueError("targets must be finite and predictions may not be infinite")
        error = np.abs(target - point)
        finite = np.isfinite(error)
        self.eligible += len(target)
        self.scored += int(finite.sum())
        self.error_sum += float(error[finite].sum())
        for minutes in self.hits:
            self.hits[minutes] += int(np.count_nonzero(error <= minutes))

    def result(self):
        n, scored = self.eligible, self.scored
        return {
            "eligible": n,
            "scored": scored,
            "missing": n - scored,
            "coverage": scored / n if n else 0.0,
            "mae_scored": self.error_sum / scored if scored else None,
            **{f"within_{m}_min": self.hits[m] / n if n else None for m in self.hits},
        }

    def sufficient(self):
        return {"eligible": self.eligible, "scored": self.scored,
                "error_sum": self.error_sum, "hits": dict(self.hits)}


def _score_table():
    return {view: {subset: Score() for subset in SUBSETS} for view in ("raw", "displayed")}


def _date_str(day_value: int) -> str:
    return str(np.datetime64(int(day_value), "D"))


def _validate_id_sequence(pred_ids, prepared_ids, label, block_rows=BLOCK_ROWS):
    if tuple(pred_ids.shape) != tuple(prepared_ids.shape) or len(pred_ids.shape) != 1:
        raise ValueError(f"{label}: ordered stop-ID shape differs from prepared rows")
    for start in range(0, len(pred_ids), block_rows):
        end = min(start + block_rows, len(pred_ids))
        if not np.array_equal(pred_ids[start:end], prepared_ids[start:end]):
            raise ValueError(f"{label}: ordered stop IDs differ from prepared rows at {start}")


def _lower_day(day_column, cutoff, n):
    low, high = 0, n
    while low < high:
        mid = (low + high) // 2
        if int(day_column[mid:mid + 1][0]) < cutoff:
            low = mid + 1
        else:
            high = mid
    return low


def _correction_status(data: PreparedSet, correction_root=CORRECTION):
    correction = data.key.get("label_correction", {})
    meta_correction = data.meta.get("label_correction", {})
    if correction.get("status") != "complete" or meta_correction != correction:
        raise ValueError("corrected evaluation cache is not complete")
    correction_result = correction_root / "result.json"
    progress_path = correction_root / "progress.json"
    if not correction_result.is_file() or not progress_path.is_file():
        raise ValueError("completed correction result/progress manifest is missing")
    result = json.loads(correction_result.read_text())
    progress = json.loads(progress_path.read_text())
    cols = correction.get("columns", {})
    status_file = data.dir / STATUS_NAME
    if not status_file.is_file() or status_file.stat().st_size != len(data):
        raise ValueError("correction status companion is absent or has the wrong row count")
    if correction.get("progress_sha") != sha256_file(progress_path) \
            or data.meta.get("label_correction", {}).get("progress_sha") != correction.get("progress_sha"):
        raise ValueError("correction progress identity differs from the prepared cache")
    status_sha = sha256_file(status_file)
    y_info = cols.get("y.bin")
    if not y_info or y_info.get("sha256") != sha256_file(data.dir / "y.bin"):
        raise ValueError("corrected target content hash differs from its manifest")
    original_identity = json.loads((correction_root / "source-inputs.json").read_text())
    original_y = original_identity["columns"]["eval"]["y.bin"]
    if original_y["sha256"] != sha256_file(ORIGINAL_PREPARED / "y.bin"):
        raise ValueError("original target column differs from the correction source manifest")

    statuses = np.memmap(status_file, dtype="u1", mode="r", shape=(len(data),))
    days = data.column("day")
    counts = {str(code): 0 for code in range(6)}
    observed_dates = []
    previous = None
    for start in range(0, len(data), BLOCK_ROWS):
        stop = min(start + BLOCK_ROWS, len(data))
        yday = np.asarray(days[start:stop])
        status = np.asarray(statuses[start:stop])
        if np.any(status > 5):
            raise ValueError("unknown label-correction status code")
        if previous is not None and len(yday) and yday[0] < previous:
            raise ValueError("prepared evaluation days are not ordered")
        if len(yday):
            previous = yday[-1]
        for code, count in zip(*np.unique(status, return_counts=True)):
            counts[str(int(code))] += int(count)
        for day in np.unique(yday):
            date = _date_str(day)
            if not observed_dates or observed_dates[-1] != date:
                observed_dates.append(date)

    # The producer hashes the exact contiguous status block for every service date.
    for date in observed_dates:
        progress_row = progress.get(date)
        if not progress_row or progress_row.get("role") != "eval":
            raise ValueError(f"correction per-date status proof is missing for {date}")
        day = int(np.datetime64(date, "D").astype(int))
        start = _lower_day(days, day, len(data))
        stop = _lower_day(days, day + 1, len(data))
        status_digest = hashlib.sha256(np.asarray(statuses[start:stop]).tobytes()).hexdigest()
        if progress_row.get("blocks_sha256", {}).get("status") != status_digest:
            raise ValueError(f"correction status proof does not match prepared date {date}")
    if result.get("prepared_root") != str((correction_root / "prepared").relative_to(ROOT)):
        raise ValueError("correction result points at another prepared-data root")
    return statuses, counts, observed_dates, {
        "correction_result_sha256": sha256_file(correction_result),
        "status_sha256": status_sha,
        "progress_sha256": sha256_file(progress_path),
        "corrected_y_sha256": y_info["sha256"],
        "original_y_sha256": original_y["sha256"],
        "status_counts": counts,
    }


def _read_json(path):
    return json.loads(Path(path).read_text())


def verify_sampler_pair(control, variant, name='weighted'):
    if control.get('version', 0) < 4 and variant.get('version', 0) < 4:
        return  # Historical report fixtures; live sources bind the current runner.
    expected_normalization = 'weighted batch mean / fixed full-training mean weight'
    records = []
    for condition, key in (('control', control), (name, variant)):
        sampler = key.get('sampler')
        if not isinstance(sampler, dict) or sampler.get('kind') != 'permuted-date-block-pools':
            raise ValueError('paired sampler metadata is absent or invalid')
        for name in ('mix_blocks', 'read_block_rows', 'pool_rows'):
            if type(sampler.get(name)) is not int or sampler[name] <= 0:
                raise ValueError('paired sampler dimensions must be positive integers')
        if sampler['mix_blocks'] < 2 or sampler['pool_rows'] != sampler['mix_blocks'] * sampler['read_block_rows'] \
                or sampler.get('row_coverage') != 'exactly once per epoch':
            raise ValueError('paired sampler does not certify bounded mixing and exact coverage')
        mean = sampler.get('dataset_mean_weight')
        limit = 1 + key.get('recency', {}).get('amplitude', 0.5)  # no weight exceeds 1 + amplitude
        if not isinstance(mean, (int, float)) or not np.isfinite(mean) or not 1 <= mean <= limit \
                or (condition == 'control' and mean != 1):
            raise ValueError('paired sampler dataset mean is invalid')
        if key.get('recency', {}).get('normalization') != expected_normalization:
            raise ValueError('paired normalization cancels temporal weight or is unrecognized')
        records.append({k: v for k, v in sampler.items() if k != 'dataset_mean_weight'})
    if records[0] != records[1]:
        raise ValueError(f'control and {name} sampler differ')


def _readiness_chromium(root: Path, track: str):
    readiness_path = experiment_directory(root) / 'readiness.json'
    if not readiness_path.is_file():
        raise ValueError(f"{track}: readiness Chromium identity is missing")
    readiness = _read_json(readiness_path)
    identity = readiness.get("chromium") if isinstance(readiness, dict) else None
    if not isinstance(identity, dict) or set(identity) != {"path", "sha256", "version"}:
        raise ValueError(f"{track}: readiness Chromium identity is incomplete")
    if not isinstance(identity["path"], str) or not Path(identity["path"]).is_absolute():
        raise ValueError(f"{track}: readiness Chromium path is invalid")
    digest = identity["sha256"]
    if not isinstance(digest, str) or len(digest) != 64 \
            or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{track}: readiness Chromium SHA-256 is invalid")
    if not isinstance(identity["version"], str) or not identity["version"].strip():
        raise ValueError(f"{track}: readiness Chromium version is invalid")
    return identity


def _check_export_and_browser(run_dir: Path, result: dict, track: str, root=ROOT):
    onnx = result.get("onnx") or {}
    if onnx.get("onnx_passed") is not True:
        raise ValueError(f"{track}: export parity did not pass")
    if result.get("within_experimental_budget") is False:
        raise ValueError(f"{track}: exported candidate exceeded its recorded experiment budget")
    browser_path = run_dir / "actual_frontend_browser.json"
    if not browser_path.is_file():
        raise ValueError(f"{track}: actual browser runtime parity result is missing")
    browser = _read_json(browser_path)
    pinned_chromium = _readiness_chromium(root, track)
    if browser.get("chromium") != pinned_chromium:
        raise ValueError(f"{track}: actual Chromium identity is missing, stale, or differs from readiness")
    if browser.get("runtime_version") != "1.30.0":
        raise ValueError(f"{track}: actual browser runtime version must be pinned ONNX Runtime Web 1.30.0")
    cases = browser.get("cases", {})
    expected_cases = {"median"} | ({"window3"} if track == "distribution" else set())
    if browser.get("passed") is not True or browser.get("error") or not expected_cases <= cases.keys():
        raise ValueError(f"{track}: actual browser runtime parity failed or is incomplete")
    required_protocol=_read_json(experiment_directory(root)/'readiness.json').get('browser_protocol',1)
    if required_protocol==2 or browser.get('parity_protocol')==2:
        if not report_passes(browser) or browser.get('output')!=('probs' if track=='distribution' else 'quantiles') \
                or browser.get('exact_quantiles')!=(track=='discrete'):
            raise ValueError(f'{track}: browser component parity protocol differs or failed')
        reference=run_dir/'browser_check/reference/reference.json'
        fixture=_read_json(reference)
        [candidate]=_read_json(run_dir/'browser_check/manifest.json')['candidates']
        identity_paths={'model':run_dir/'browser_check'/candidate/'model.onnx',
                        'prep':run_dir/'browser_check'/candidate/'prep.json','reference':reference}
        for name,spec in (('native_raw',fixture['expected'][candidate]['raw']),
                          ('cat',fixture['inputs']['cat_robust']),('num',fixture['inputs']['num_robust'])):
            identity_paths[name]=reference.parent/spec['file']
        if browser.get('fixture_identity')!={name:sha256_file(path) for name,path in identity_paths.items()} \
                or browser.get('checker_sha256')!=sha256_file(root/'scripts/check_recency_browser.py'):
            raise ValueError(f'{track}: browser fixture/model/checker identity differs')
        rows=fixture['inputs']['cat_robust']['shape'][0]
        if browser.get('fixture_rows')!=rows or any(case['rows']!=rows for case in cases.values()):
            raise ValueError(f'{track}: browser rows differ from the frozen input fixture')
    else:
        for name in expected_cases:
            case = cases[name]
            if case.get("max_abs_vs_python", float("inf")) > 1e-2 \
                    or case.get("single_vs_batch_max_abs", float("inf")) > 1e-3 \
                    or case.get("wrong_shape", 1) != 0 \
                    or case.get("single_unknown_categories_checked") is not True \
                    or case.get("rows", 0) < 1:
                raise ValueError(f"{track}: browser parity details fail for {name}")
    artifact_info = result.get("artifact") or {}
    rel = artifact_info.get("path")
    if not rel:
        raise ValueError(f"{track}: exported artifact manifest path is missing")
    artifact = (root / rel).resolve()
    if root.resolve() not in artifact.parents:
        raise ValueError(f"{track}: artifact path leaves the repository")
    manifest_path = artifact / "manifest.json"
    manifest = _read_json(manifest_path)
    model_name = manifest.get("model_file")
    if not model_name or not (artifact / model_name).is_file():
        raise ValueError(f"{track}: manifest model file is absent")
    model_path = artifact / model_name
    model_bytes = model_path.stat().st_size
    if manifest.get("asset_bytes") != model_bytes:
        raise ValueError(f"{track}: manifest asset size does not match the model file")
    measured = model_bytes + manifest_path.stat().st_size
    if measured != manifest.get("download_bytes") or measured != artifact_info.get("download_bytes"):
        raise ValueError(f"{track}: manifest-inclusive download size does not reconcile")
    return {
        "onnx_passed": True,
        "browser_passed": True,
        "browser_chromium": browser.get("chromium"),
        "browser_runtime_version": browser.get("runtime_version"),
        "browser_cases": {name: cases[name] for name in sorted(expected_cases)},
        "artifact_path": str(artifact.relative_to(root.resolve())),
        "artifact_role": manifest.get("role"),
        "manifest_inclusive_download_bytes": measured,
        "strict_35mb_passed": measured <= 35_000_000,
        "onnx_bytes": result.get("onnx_bytes"),
        "model_sha256": sha256_file(model_path),
        "manifest_sha256": sha256_file(manifest_path),
        "browser_result_sha256": sha256_file(browser_path),
    }


def _verify_run(run_dir: Path, condition: str, track: str, prepared: PreparedSet, expected_tail_rows: int,
                *, validate_provenance=True):
    key_path, result_path = run_dir / "key.json", run_dir / "result.json"
    if not key_path.is_file() or not result_path.is_file():
        raise ValueError(f"{condition}/{track}: completed key/result are required")
    key, result = _read_json(key_path), _read_json(result_path)
    if track == "discrete":
        source_key = key.get("source_key", {})
        run_condition = source_key.get("condition")
        run_mode = source_key.get("mode")
        validation_key = source_key.get("validation")
        prepared_root = source_key.get("prepared_root")
    else:
        run_condition, run_mode = key.get("condition"), key.get("mode")
        validation_key, prepared_root = key.get("validation"), key.get("prepared_root")
    if run_condition != condition or (track != "discrete" and run_mode != "train"):
        raise ValueError(f"{condition}/{track}: run condition or completion mode differs")
    if result.get("track") != track:
        raise ValueError(f"{condition}/{track}: result track differs")
    expected_prepared = PREPARED.parent.relative_to(ROOT).as_posix()
    if prepared_root != expected_prepared:
        raise ValueError(f"{condition}/{track}: run did not use the corrected prepared tree")
    if validation_key != prepared.key:
        raise ValueError(f"{condition}/{track}: corrected validation key differs")
    if validate_provenance and track != "discrete":
        verify_training_key(key, ROOT)
    if not result.get("prediction_identity") or not result.get("later_probe"):
        raise ValueError(f"{condition}/{track}: later-comparison arrays/metrics are missing")
    identity = result["prediction_identity"]
    ids_path = run_dir / "stop_ids.npy"
    if identity.get("stop_ids_sha256") != sha256_file(ids_path):
        raise ValueError(f"{condition}/{track}: ordered stop-ID companion hash differs")
    ids = np.load(ids_path, mmap_mode="r", allow_pickle=False)
    if ids.dtype != np.dtype("<i8") or ids.shape != (expected_tail_rows,):
        raise ValueError(f"{condition}/{track}: ordered stop-ID shape/dtype differs")
    if track == "fine":
        arr_name = "median.npy"
        probe_name = "median"
    elif track == "distribution":
        arr_name = "window3.npy"
        probe_name = "window3"
    else:
        arr_name = "median.npy"
        probe_name = "median"
    expected_arrays = identity.get("arrays", {})
    if set(expected_arrays) != {arr_name}:
        raise ValueError(f"{condition}/{track}: prediction array manifest has unexpected or missing arrays")
    if expected_arrays.get(arr_name) != sha256_file(run_dir / arr_name):
        raise ValueError(f"{condition}/{track}: prediction array content hash differs")
    points = np.load(run_dir / arr_name, mmap_mode="r", allow_pickle=False)
    if points.dtype != np.dtype("float32") or points.shape != (expected_tail_rows,):
        raise ValueError(f"{condition}/{track}: prediction array shape/dtype differs")
    if arr_name not in expected_arrays:
        raise ValueError(f"{condition}/{track}: prediction array hash is absent from result manifest")
    probe = result["later_probe"].get(probe_name)
    if track == "discrete":
        probe = result["later_probe"].get("median", probe)
    probe_name = "median" if track == "discrete" else probe_name
    # Check both the point-score denominator and the full-cohort percentile
    # diagnostics emitted by the frozen producer. Point arrays alone cannot
    # reconstruct interval coverage or percentile ordering.
    if not isinstance(probe, dict) or probe.get("overall", {}).get("eligible") != expected_tail_rows:
        raise ValueError(f"{condition}/{track}: saved later-comparison metric row count differs")
    calibration = _validated_later_probe(result, probe_name, expected_tail_rows,
                                         f"{condition}/{track}")
    if track != "discrete":
        history_path = run_dir / "history.json"
        history_sha = result.get("history_sha256")
        if not history_sha or not history_path.is_file() or history_sha != sha256_file(history_path):
            raise ValueError(f"{condition}/{track}: completed history hash is absent or differs")
    export = _check_export_and_browser(run_dir, result, track, ROOT)
    if not result.get("resources"):
        raise ValueError(f"{condition}/{track}: run resource record is missing")
    fit_key = key.get("source_key") if track == "discrete" else key
    return {"key": key, "fit_key": fit_key, "result": result, "ids": ids, "points": points,
            "array_name": arr_name, "export": export, "calibration": calibration}


def _verify_discrete_source(run_dir: Path, condition: str, source_distribution: dict, root=ROOT):
    key = _read_json(run_dir / "key.json")
    result = _read_json(run_dir / "result.json")
    if key.get("script_sha") != sha256_file(root / "scripts/check_recency_discrete_delay.py") \
            or key.get("provenance_sha") != sha256_file(root / "scripts/recency_provenance.py"):
        raise ValueError(f"{condition}/discrete: postprocessor or provenance guard changed")
    source_key = key.get("source_key")
    if source_key != source_distribution["key"]:
        raise ValueError(f"{condition}/discrete: source classifier key differs from distribution run")
    weights_name = f"epoch-{key.get('selected_epoch', 0):03d}.pt"
    source_dir = EXPERIMENT / condition / "runs/distribution"
    if key.get("source_weights_sha") != sha256_file(source_dir / weights_name):
        raise ValueError(f"{condition}/discrete: selected immutable classifier checkpoint hash differs")
    history_path = source_dir / "history.json"
    completed_history_sha = source_distribution["result"].get("history_sha256")
    if not completed_history_sha or completed_history_sha != sha256_file(history_path):
        raise ValueError(f"{condition}/discrete: distribution history differs from its completed result")
    history = _read_json(history_path)["epochs"]
    epoch_record = next((entry for entry in history if entry["epoch"] == key["selected_epoch"]), None)
    if not epoch_record or epoch_record.get("checkpoint_sha256") != key["source_weights_sha"]:
        raise ValueError(f"{condition}/discrete: checkpoint hash differs from training-time epoch record")
    if result.get("source_weights_sha") not in (None, key["source_weights_sha"]):
        raise ValueError(f"{condition}/discrete: result checkpoint identity differs")
    if result.get("selected_epoch") != key["selected_epoch"]:
        raise ValueError(f"{condition}/discrete: selected epoch differs from its source key")
    for relpath, expected in result.get("export_identity", {}).items():
        path = (root / relpath).resolve()
        if root.resolve() not in path.parents or sha256_file(path) != expected:
            raise ValueError(f"{condition}/discrete: exported content identity differs: {relpath}")
    if not result.get("export_identity"):
        raise ValueError(f"{condition}/discrete: exported content identity is absent")


def _new_metric_tree():
    tree = defaultdict(lambda: defaultdict(lambda: defaultdict(Score)))
    for subset in SUBSETS:
        tree[subset]["overall"]["all"] = Score()
    return tree


def _metric_result(tree):
    return {subset: {dimension: {key: score.result() for key, score in groups.items()}
                     for dimension, groups in dimensions.items()}
            for subset, dimensions in tree.items()}


def _dimensions_from_lookup(conn, stop_ids, day_values, segment_values, segment_dictionary,
                            block_rows=BLOCK_ROWS, sql_limit=SQL_IDS):
    """Yield ordered state/source/route/trip metadata using indexed bounded ID lists."""
    required = {
        "trip_stop": {"id", "trip_id", "station_id"},
        "trip": {"id", "train_id", "service_date", "source"},
        "train": {"id", "route_id", "segment"},
        "station": {"id", "state"},
    }
    for table, columns in required.items():
        found = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not columns <= found:
            raise ValueError(f"source database {table} schema lacks required group columns")
    for begin in range(0, len(stop_ids), block_rows):
        end = min(begin + block_rows, len(stop_ids))
        ids = np.asarray(stop_ids[begin:end], dtype=np.int64)
        rows = {}
        for offset in range(0, len(ids), sql_limit):
            part = ids[offset:offset + sql_limit]
            marks = ",".join("?" for _ in part)
            query = ("SELECT ts.id, ts.trip_id, t.service_date, s.state, t.source, tr.route_id, tr.segment "
                     "FROM trip_stop AS ts JOIN trip AS t ON t.id=ts.trip_id "
                     "LEFT JOIN station AS s ON s.id=ts.station_id "
                     "LEFT JOIN train AS tr ON tr.id=t.train_id "
                     f"WHERE ts.id IN ({marks})")
            for row in conn.execute(query, [int(x) for x in part]):
                row_id = int(row[0])
                if row_id in rows:
                    raise ValueError(f"source database returned duplicate trip_stop id {row_id}")
                rows[row_id] = row
        if len(rows) != len(ids):
            raise ValueError(f"source database returned {len(rows)} of {len(ids)} prepared stop IDs")
        states, sources, routes, trips, services, segment_mismatches = [], [], [], [], [], []
        for index, row_id in enumerate(ids):
            trip_id, service_date, state, source, route_id, database_segment = rows[int(row_id)][1:]
            day_date = _date_str(day_values[begin + index])
            if service_date != day_date:
                raise ValueError(f"trip_stop {int(row_id)} service_date {service_date} differs from prepared {day_date}")
            states.append(state or "UNKNOWN")
            sources.append(source or "UNKNOWN")
            routes.append("UNKNOWN" if route_id is None else str(int(route_id)))
            trips.append(str(int(trip_id)))
            code = int(segment_values[begin + index])
            if not 0 <= code < len(segment_dictionary):
                raise ValueError(f"prepared segment code {code} is outside its frozen dictionary")
            prepared_segment = segment_dictionary[code]
            segment_mismatches.append(database_segment not in (None, "")
                                      and prepared_segment not in (None, "", database_segment))
            services.append({"RV": "Regional", "FV": "Long-distance", "S": "S-Bahn"}.get(prepared_segment, "UNKNOWN"))
        yield begin, end, {
            "state": np.asarray(states, dtype=object),
            "source": np.asarray(sources, dtype=object),
            "route": np.asarray(routes, dtype=object),
            "trip_id": np.asarray(trips, dtype=object),
            "service": np.asarray(services, dtype=object),
            "segment_mismatch": np.asarray(segment_mismatches, dtype=bool),
        }


def _preflight_group_cardinality(conn, prepared, start, stop, segment_dictionary, conditions=2):
    """Count distinct groups on disk before allocating Python per-group scores."""
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-32768")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS summary_group_keys (dimension TEXT, label TEXT, PRIMARY KEY(dimension,label)) WITHOUT ROWID")
    conn.execute("DELETE FROM summary_group_keys")
    unknown_route_rows = 0
    unknown_route_by_source = defaultdict(int)
    segment_mismatch_rows = 0
    segment_mismatch_by_source = defaultdict(int)
    total_rows = 0
    ids = prepared.column("stop_id")[start:stop]
    days = prepared.column("day")[start:stop]
    segments = prepared.column("segment")[start:stop]
    for begin, end, dimensions in _dimensions_from_lookup(conn, ids, days, segments, segment_dictionary):
        dates = [_date_str(x) for x in days[begin:end]]
        groups = {"service_date": dates,
                  **{name: dimensions[name].tolist() for name in ("service", "state", "source", "route")}}
        for dimension, labels in groups.items():
            conn.executemany("INSERT OR IGNORE INTO summary_group_keys VALUES (?,?)",
                             ((dimension, str(label)) for label in set(labels)))
        unknown_route_rows += sum(label == "UNKNOWN" for label in dimensions["route"])
        for source, route in zip(dimensions["source"], dimensions["route"]):
            if route == "UNKNOWN":
                unknown_route_by_source[str(source)] += 1
        for source, mismatch in zip(dimensions["source"], dimensions["segment_mismatch"]):
            if mismatch:
                segment_mismatch_rows += 1
                segment_mismatch_by_source[str(source)] += 1
        total_rows += end - begin
    counts = {name: int(count) for name, count in conn.execute(
        "SELECT dimension,COUNT(*) FROM summary_group_keys GROUP BY dimension")}
    # Each unique label is retained in 18 Score records per condition (3 views ×
    # 2 raw/displayed × 3 subsets). Use a conservative per-record estimate and
    # leave headroom for NumPy blocks, SQLite buffers, and the Python runtime.
    records = 18 * conditions
    estimated_score_bytes = (sum(counts.values()) * records + records) * 1024
    if estimated_score_bytes > 1_000 * 2**20:
        raise ValueError(f"group-score estimate {estimated_score_bytes} bytes exceeds the 1 GiB safety budget")
    return {"unique_labels_by_dimension": counts,
            "estimated_score_aggregate_bytes": estimated_score_bytes,
            "unknown_route_rows": unknown_route_rows,
            "unknown_route_rows_by_source": dict(unknown_route_by_source),
            "prepared_database_segment_mismatch_rows": segment_mismatch_rows,
            "prepared_database_segment_mismatch_by_source": dict(segment_mismatch_by_source),
            "route_rows": total_rows}


def _score_close(actual, expected, label):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise ValueError(f"{label}: baseline metric fields differ")
        for key, value in expected.items():
            _score_close(actual[key], value, f"{label}.{key}")
    elif expected is None:
        if actual is not None:
            raise ValueError(f"{label}: baseline expected null, got {actual}")
    elif isinstance(expected, (int, np.integer)):
        if actual != int(expected):
            raise ValueError(f"{label}: baseline count differs")
    elif not np.isclose(float(actual), float(expected), rtol=1e-10, atol=1e-12):
        raise ValueError(f"{label}: baseline score differs")


def _verify_correction_baseline(root, prepared, statuses, start, stop, pred_start, correction_identity):
    base = root / DEFAULT_RELATIVE / "correction_baselines"
    key_path, result_path = base / "key.json", base / "result.json"
    if not key_path.is_file() or not result_path.is_file():
        raise ValueError("completed unchanged-checkpoint correction baseline key/result are required")
    key, result = _read_json(key_path), _read_json(result_path)
    if result.get("key_sha") != sha256_file(key_path):
        raise ValueError("correction baseline result does not bind its key")
    baseline_script = root / "scripts/score_label_correction_baselines.py"
    if key.get("script_sha") != sha256_file(baseline_script):
        raise ValueError("correction baseline producer script differs from its frozen key")
    if key.get("correction_result_sha") != correction_identity["correction_result_sha256"]:
        raise ValueError("correction baseline used another correction result")
    if key.get("prepared_key") != prepared.key or key.get("status_sha") != correction_identity["status_sha256"]:
        raise ValueError("correction baseline does not match the verified prepared labels/status")
    expected_tail_rows = len(prepared) - pred_start
    if result.get("rows") != expected_tail_rows:
        raise ValueError("correction baseline row count differs from the later validation tail")
    array_hashes = result.get("arrays", {})
    expected_names = {"reference.npy", "fine.npy", "distribution.npy", "discrete.npy", "stop_ids.npy"}
    if set(array_hashes) != expected_names:
        raise ValueError("correction baseline prediction array list differs")
    for name, expected_sha in array_hashes.items():
        path = base / name
        if not path.is_file() or sha256_file(path) != expected_sha:
            raise ValueError(f"correction baseline array hash differs: {name}")
    if key.get("reference_sha") != sha256_file(root / "data/ml/full_year/runs/compact-2026-09-26/trials/f1ac12b1742e3a0b/validation.npz"):
        raise ValueError("correction baseline reference prediction source differs")
    old_root = root / "data/ml/compact_training_20261013/runs"
    for name in ("fine", "distribution", "discrete"):
        record = key.get("graphs", {}).get(name, {})
        for field, rel in (("onnx_sha", old_root / name / "onnx/model.onnx"),
                           ("prep_sha", old_root / name / "onnx/prep.json")):
            if not record.get(field) or record[field] != sha256_file(rel):
                raise ValueError(f"correction baseline original {name} {field} differs")
    ids = np.load(base / "stop_ids.npy", mmap_mode="r", allow_pickle=False)
    if ids.dtype != np.dtype("<i8") or ids.shape != (expected_tail_rows,):
        raise ValueError("correction baseline ordered stop-ID shape/dtype differs")
    for offset in range(0, expected_tail_rows, BLOCK_ROWS):
        end = min(offset + BLOCK_ROWS, expected_tail_rows)
        if not np.array_equal(ids[offset:end], prepared.column("stop_id")[pred_start + offset:pred_start + end]):
            raise ValueError("correction baseline ordered stop IDs differ from corrected prepared rows")

    original_path = root / "data/ml/full_year/prepared" / f"{PREFIX}eval/y.bin"
    original_sha = _read_json(root / CORRECTION.relative_to(ROOT) / "source-inputs.json")["columns"]["eval"]["y.bin"]["sha256"]
    if sha256_file(original_path) != original_sha:
        raise ValueError("correction baseline original evaluation labels differ from frozen source")
    old_y = np.memmap(original_path, dtype="<f4", mode="r", shape=(len(prepared),))
    labels = ("original", "corrected", "corrected_verified", "corrected_unresolved")
    names = ("reference", "fine", "distribution", "discrete")
    full_trees = {name: {label: {view: Score() for view in ("raw", "displayed")} for label in labels}
                  for name in names}
    cohort_trees = {name: {label: {view: Score() for view in ("raw", "displayed")} for label in labels}
                    for name in names}
    prediction_arrays = {name: np.load(base / f"{name}.npy", mmap_mode="r", allow_pickle=False)
                         for name in names}
    if any(array.dtype != np.dtype("<f4") or array.shape != (expected_tail_rows,)
           for array in prediction_arrays.values()):
        raise ValueError("correction baseline prediction array shape/dtype differs")
    for offset in range(0, expected_tail_rows, BLOCK_ROWS):
        a, b = pred_start + offset, min(len(prepared), pred_start + offset + BLOCK_ROWS)
        target_old = np.asarray(old_y[a:b], dtype=np.float32)
        target_new = np.asarray(prepared.y[a:b], dtype=np.float32)
        status = np.asarray(statuses[a:b], dtype=np.uint8)
        verified = np.isin(status, (1, 2))
        full_masks = {"original": (target_old, np.ones(b-a, dtype=bool)),
                      "corrected": (target_new, np.ones(b-a, dtype=bool)),
                      "corrected_verified": (target_new, verified),
                      "corrected_unresolved": (target_new, ~verified)}
        cohort_a, cohort_b = max(a, start), min(b, stop)
        if cohort_a < cohort_b:
            left, right = cohort_a - a, cohort_b - a
            cohort_masks = {name: (target[left:right], mask[left:right])
                            for name, (target, mask) in full_masks.items()}
        else:
            cohort_masks = {}
        for name, points in prediction_arrays.items():
            prediction = np.asarray(points[offset:offset + (b-a)], dtype=np.float32)
            if not np.isfinite(prediction).all():
                raise ValueError(f"correction baseline {name} prediction contains nonfinite values")
            for label, (target, mask) in full_masks.items():
                full_trees[name][label]["raw"].add(target[mask], prediction[mask])
                full_trees[name][label]["displayed"].add(target[mask], shown(prediction[mask]))
            if cohort_masks:
                cohort_prediction = prediction[cohort_a-a:cohort_b-a]
                for label, (target, mask) in cohort_masks.items():
                    cohort_trees[name][label]["raw"].add(target[mask], cohort_prediction[mask])
                    cohort_trees[name][label]["displayed"].add(target[mask], shown(cohort_prediction[mask]))
    saved_check = {name: {label: {view: score.result() for view, score in views.items()}
                          for label, views in labels_tree.items()}
                   for name, labels_tree in full_trees.items()}
    cohort_scores = {name: {label: {view: score.result() for view, score in views.items()}
                            for label, views in labels_tree.items()}
                     for name, labels_tree in cohort_trees.items()}
    saved = result.get("scores", {})
    for name, labels_tree in saved_check.items():
        for label, views in labels_tree.items():
            for view, values in views.items():
                _score_close(saved.get(name, {}).get(label, {}).get(view), values,
                             f"correction baseline {name}/{label}/{view}")
    effects = {}
    for name, labels_tree in cohort_scores.items():
        effects[name] = {}
        for view in ("raw", "displayed"):
            before, after = labels_tree["original"][view], labels_tree["corrected"][view]
            effects[name][view] = {
                "original": before,
                "corrected": after,
                "correction_minus_original": {
                    metric: (None if before[metric] is None or after[metric] is None
                             else after[metric] - before[metric])
                    for metric in ("within_1_min", "within_3_min", "within_5_min", "mae_scored")
                },
            }
    return {"result_sha256": sha256_file(result_path), "key_sha256": sha256_file(key_path),
            "full_later_tail_rows": result["rows"], "full_later_tail_scores_verified": result["scores"],
            "cohort_rows": stop - start, "cohort_scores": cohort_scores, "effects": effects,
            "scope": result.get("scope"), "ordered_stop_ids_sha256": array_hashes["stop_ids.npy"]}


def _verify_corrected_august(root, correction_identity):
    base = root / DEFAULT_RELATIVE / "corrected_august_comparison"
    result_path = base / "result.json"
    if not result_path.is_file():
        raise ValueError("mandatory corrected August comparison result is absent")
    result = _read_json(result_path)
    source_script = root / "scripts/compare_corrected_august.py"
    if not source_script.is_file() or result.get("script_sha") != sha256_file(source_script):
        raise ValueError("corrected August comparison script identity differs")
    if result.get("correction_result_sha") != correction_identity["correction_result_sha256"]:
        raise ValueError("corrected August comparison used another correction version")
    if result.get("reference") != "August1–31 only, one vote per recurring trip/date with fixed common stops" \
            or result.get("label_policy") != "verified corrected targets overlaid; uncovered rows retain explicitly unverified originals":
        raise ValueError("corrected August comparison reference or target-overlay policy differs")
    source_matrix = root / "data/ml/delay_shift_audit_20261013/matched-delays.f32"
    if not source_matrix.is_file() or result.get("original_matrix_sha") != sha256_file(source_matrix):
        raise ValueError("corrected August comparison original matched matrix identity differs")
    publication = _read_json(root / "data/ml/delay_shift_audit_20261013/publication-verification.json")
    matrix_name = "data/ml/delay_shift_audit_20261013/matched-delays.f32"
    if publication.get("content_identities", {}).get(matrix_name, {}).get("sha256") != result.get("original_matrix_sha"):
        raise ValueError("corrected August source matrix is not covered by the original publication verification")
    for name, expected in result.get("files", {}).items():
        path = base / name
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"corrected August result file hash differs: {name}")
    dates = [f"2026-08-{day:02d}" for day in range(1, 32)] + [f"2026-09-{day:02d}" for day in range(6, 20)]
    coverage = result.get("coverage", [])
    if [row.get("date") for row in coverage] != dates:
        raise ValueError("corrected August comparison coverage does not preserve its frozen 45-date population")
    for row in coverage:
        total = row.get("original_observed_matched_stops", -1)
        if not 0 <= row.get("source_verified_stops", -1) <= row.get("in_corrected_cache", -1) <= total \
                or row.get("unverified_retained_original_stops") != total - row.get("source_verified_stops"):
            raise ValueError(f"corrected August coverage counts do not reconcile: {row.get('date')}")
    august_22 = coverage[21]
    if august_22["source_verified_stops"] != 0 or august_22["unverified_retained_original_stops"] != august_22["original_observed_matched_stops"]:
        raise ValueError("August 22 rows must remain explicitly unverified in the corrected overlay")
    with (base / "trip-comparison.csv").open(newline="") as stream:
        supported = [row for row in csv.DictReader(stream) if row.get("supported", "").lower() == "true"]
    deviations = np.asarray([float(row["deviation_min"]) for row in supported], dtype=np.float64)
    if len(supported) != result.get("supported_trips") or not len(deviations):
        raise ValueError("corrected August supported-trip count differs from its CSV")
    checks = {"mean_deviation_min": float(deviations.mean()),
              "median_deviation_min": float(np.median(deviations)),
              "shares": {f"over_{threshold}_min": float((deviations > threshold).mean())
                        for threshold in (5, 15, 30)}}
    _score_close({"mean_deviation_min": result.get("mean_deviation_min"),
                  "median_deviation_min": result.get("median_deviation_min"),
                  "shares": result.get("shares")}, checks, "corrected August summary")
    return {"status": "verified", "result_sha256": sha256_file(result_path),
            "script_sha256": sha256_file(source_script),
            "reference": result.get("reference"), "label_policy": result.get("label_policy"),
            "correction_result_sha256": result["correction_result_sha"],
            "supported_trips": result.get("supported_trips"),
            "mean_deviation_min": result.get("mean_deviation_min"),
            "median_deviation_min": result.get("median_deviation_min"),
            "shares": result.get("shares"), "coverage": coverage,
            "limitations": result.get("limitations"), "files": result.get("files")}


class TripDateAccumulator:
    """Disk-backed per-trip/date sufficient stats; memory stays block-bounded."""

    MODELS = ("fine", "window3", "discrete")

    def __init__(self, path: Path, conditions=DEFAULT_CONDITIONS):
        self.conditions = tuple(conditions)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA temp_store=FILE")
        self.conn.execute("PRAGMA cache_size=-32768")
        self.columns = []
        for condition in self.conditions:
            for model in self.MODELS:
                self.columns.extend([f"{condition}_{model}_n", f"{condition}_{model}_error",
                                     f"{condition}_{model}_within5"])
        definitions = ",".join(f'"{column}" REAL NOT NULL DEFAULT 0' for column in self.columns)
        self.conn.execute(f"CREATE TABLE trips (trip_id TEXT PRIMARY KEY, {definitions})")
        placeholders = ",".join("?" for _ in range(len(self.columns) + 1))
        updates = ",".join(f'"{col}"="{col}"+excluded."{col}"' for col in self.columns)
        self.insert_sql = (f"INSERT INTO trips VALUES ({placeholders}) ON CONFLICT(trip_id) DO UPDATE SET {updates}")

    def add(self, trip_ids, target, points):
        unique, inverse = np.unique(trip_ids, return_inverse=True)
        n = np.bincount(inverse, minlength=len(unique)).astype(np.float64)
        block_columns = {column: None for column in self.columns}
        rows = []
        for condition in self.conditions:
            for model in self.MODELS:
                point = np.asarray(points[condition][model], dtype=np.float64)
                error = np.abs(np.asarray(target, dtype=np.float64) - shown(point))
                prefix = f"{condition}_{model}"
                block_columns[f"{prefix}_n"] = n
                block_columns[f"{prefix}_error"] = np.bincount(inverse, weights=error, minlength=len(unique))
                block_columns[f"{prefix}_within5"] = np.bincount(inverse, weights=(error <= 5), minlength=len(unique))
        for index, trip_id in enumerate(unique):
            rows.append((str(trip_id), *(float(block_columns[column][index]) for column in self.columns)))
        self.conn.executemany(self.insert_sql, rows)

    def result(self):
        output = {}
        for model in self.MODELS:
            c_n, c_error, c_hits = (f'control_{model}_{suffix}' for suffix in ("n", "error", "within5"))
            count = self.conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
            output[model] = {"trip_date_clusters": count, "metrics": {}}
            for variant in self.conditions[1:]:
                w_n, w_error, w_hits = (f'{variant}_{model}_{suffix}' for suffix in ("n", "error", "within5"))
                mismatch = self.conn.execute(
                    f'SELECT COUNT(*) FROM trips WHERE "{c_n}" != "{w_n}" OR "{c_n}" <= 0').fetchone()[0]
                if mismatch or not count:
                    raise ValueError("paired trip-date groups have unequal, empty, or absent rows")
                for metric, c_value, w_value, direction in (
                        ("within_5_min_displayed", f'"{c_hits}"/"{c_n}"', f'"{w_hits}"/"{w_n}"', ">"),
                        ("mae_displayed", f'"{c_error}"/"{c_n}"', f'"{w_error}"/"{w_n}"', "<")):
                    means = self.conn.execute(f"SELECT AVG({c_value}), AVG({w_value}) FROM trips").fetchone()
                    delta_expr = f"({w_value})-({c_value})"
                    improved_expr = f"CASE WHEN {delta_expr} {direction} 0 THEN 1.0 ELSE 0.0 END"
                    improved = self.conn.execute(f"SELECT AVG({improved_expr}) FROM trips").fetchone()[0]
                    lo_offset, hi_offset = (count - 1) // 2, count // 2
                    ordered = f"SELECT {delta_expr} FROM trips ORDER BY {delta_expr}"
                    lo = self.conn.execute(f"{ordered} LIMIT 1 OFFSET ?", (lo_offset,)).fetchone()[0]
                    hi = self.conn.execute(f"{ordered} LIMIT 1 OFFSET ?", (hi_offset,)).fetchone()[0]
                    median = (lo + hi) / 2
                    output[model]["metrics"].setdefault(metric, {"control_equal_cluster_mean": float(means[0])}).update({
                        f"{variant}_equal_cluster_mean": float(means[1]),
                        f"{variant}_minus_control_mean": float(means[1] - means[0]),
                        f"{variant}_paired_delta_median": float(median),
                        f"{variant}_clusters_improved_share": float(improved),
                    })
        return output

    def close(self):
        self.conn.close()


def _load_candidate_sets(root=ROOT, prepared=None, conditions=None):
    conditions = tuple(conditions or experiment_conditions(EXPERIMENT))
    if prepared is None:
        prepared = PreparedSet(PREPARED)
    day = prepared.column("day")
    start = _lower_day(day, int(np.datetime64(START_DAY, "D").astype(int)), len(prepared))
    stop = _lower_day(day, int(np.datetime64(END_DAY, "D").astype(int)), len(prepared))
    if stop <= start:
        raise ValueError("corrected later-comparison cohort September 6–19 is absent")
    # Verify the inclusive service-date range has every calendar day and no others.
    day_set = set()
    for pos in range(start, stop, BLOCK_ROWS):
        day_set.update(_date_str(x) for x in np.unique(day[pos:min(pos + BLOCK_ROWS, stop)]))
    expected_days = [str(np.datetime64(START_DAY, "D") + np.timedelta64(i, 'D')) for i in range(14)]
    if sorted(day_set) != expected_days:
        raise ValueError("corrected evaluation does not contain the exact 14 service dates September 6–19")
    pred_start = _lower_day(day, int(np.datetime64(START_DAY, "D").astype(int)), len(prepared))
    tail_rows = len(prepared) - pred_start
    candidate_sets = {condition: {} for condition in conditions}
    for condition in candidate_sets:
        base = EXPERIMENT / condition / "runs"
        for track in ("fine", "distribution"):
            candidate_sets[condition][track] = _verify_run(base / track, condition, track, prepared, tail_rows)
        discrete_dir = base / "discrete"
        candidate_sets[condition]["discrete"] = _verify_run(discrete_dir, condition, "discrete", prepared, tail_rows)
        _verify_discrete_source(discrete_dir, condition, candidate_sets[condition]["distribution"], root)
        for track, candidate in candidate_sets[condition].items():
            # Each candidate's full later-window ID array is compared to the same prepared rows.
            ids = candidate["ids"]
            for pos in range(0, tail_rows, BLOCK_ROWS):
                end = min(pos + BLOCK_ROWS, tail_rows)
                if not np.array_equal(ids[pos:end], prepared.column("stop_id")[pred_start + pos:pred_start + end]):
                    raise ValueError(f"{condition}/{track}: ordered IDs differ from corrected prepared evaluation")
            candidate["prediction_start"] = pred_start
    for variant in conditions[1:]:
        amplitude, trial = CONDITIONS[variant]
        for track in ("fine", "distribution", "discrete"):
            ctl, wgt = candidate_sets["control"][track], candidate_sets[variant][track]
            verify_sampler_pair(ctl['fit_key'], wgt['fit_key'], variant)
            shared_fields = ("config", "selection", "fit", "fit_metadata", "validation", "validation_metadata",
                             "fit_files", "validation_files", "production_code", "input_sha",
                             "reference_predictions_sha", "warm_start_sha", "precision_limit", "device")
            if trial != NO_DOY_TRIAL:  # its own inputs and warm start, bound by verify_training_key
                shared_fields = tuple(f for f in shared_fields if f not in ("input_sha", "warm_start_sha"))
            for field in shared_fields:
                if ctl["fit_key"].get(field) != wgt["fit_key"].get(field):
                    raise ValueError(f"{track}: control and {variant} {field} differ")
            c_policy, w_policy = ctl["fit_key"]["recency"], wgt["fit_key"]["recency"]
            if c_policy.get("amplitude") != 0 or w_policy.get("amplitude") != amplitude \
                    or c_policy.get("start") != w_policy.get("start") or c_policy.get("end") != w_policy.get("end") \
                    or ctl["fit_key"].get("trial", str(NO_DOY_TRIAL)) != str(NO_DOY_TRIAL) \
                    or wgt["fit_key"].get("trial", str(NO_DOY_TRIAL)) != str(trial):
                raise ValueError(f"{track}: recency policy or input trial differs from the frozen control/{variant} comparison")
    # Scoring names the distribution decision rule by its public view. Add the
    # alias only after its saved files and paired fit identities have passed.
    for condition in candidate_sets:
        candidate_sets[condition]["window3"] = candidate_sets[condition]["distribution"]
    return candidate_sets, start, stop, pred_start, tail_rows, expected_days


def _accumulate(candidate_sets, prepared, statuses, start, stop, pred_start, conn, trip_db_path,
                segment_dictionary):
    model_names = ("fine", "window3", "discrete")
    conditions = tuple(candidate_sets)
    all_scores = {c: {v: {m: _new_metric_tree() for m in model_names}
                      for v in ("raw", "displayed")}
                  for c in conditions}
    day_sums = {c: {m: {v: {} for v in ("raw", "displayed")}
                    for m in ("fine", "window3", "discrete")}
                for c in conditions}
    trip_ids_by_day = defaultdict(set)
    cohort_status_counts = {str(code): 0 for code in range(6)}
    trip_acc = TripDateAccumulator(trip_db_path, conditions)
    for data_start, data_stop, dimensions in _dimensions_from_lookup(
            conn, prepared.column("stop_id")[start:stop], prepared.column("day")[start:stop],
            prepared.column("segment")[start:stop], segment_dictionary):
        a, b = start + data_start, start + data_stop
        offset_a, offset_b = a - pred_start, b - pred_start
        y = np.asarray(prepared.y[a:b], dtype=np.float64)
        status = np.asarray(statuses[a:b], dtype=np.uint8)
        for code, count in zip(*np.unique(status, return_counts=True)):
            cohort_status_counts[str(int(code))] += int(count)
        day_values = np.asarray(prepared.column("day")[a:b], dtype=np.int32)
        group_values = {
            "service_date": np.asarray([_date_str(x) for x in day_values], dtype=object),
            "service": dimensions["service"],
            **{name: dimensions[name] for name in ("state", "source", "route")},
        }
        for date, trip_id in zip(group_values["service_date"], dimensions["trip_id"]):
            trip_ids_by_day[str(date)].add(str(trip_id))
        trip_points = {condition: {model: candidate_sets[condition][model]["points"][offset_a:offset_b]
                                   for model in model_names}
                       for condition in conditions}
        trip_acc.add(dimensions["trip_id"], y, trip_points)
        masks = {
            "complete": np.ones(len(y), dtype=bool),
            "verified_1_2": (status == 1) | (status == 2),
            "unresolved_0_3_4_5": np.isin(status, (0, 3, 4, 5)),
        }
        for condition in conditions:
            for model_name in ("fine", "window3", "discrete"):
                points = candidate_sets[condition][model_name]["points"][offset_a:offset_b]
                if not np.isfinite(points).all():
                    raise ValueError(f"{condition}/{model_name}: later prediction contains missing values")
                for view, prediction in (("raw", points), ("displayed", shown(points))):
                    tree = all_scores[condition][view][model_name]
                    for subset, mask in masks.items():
                        tree[subset]["overall"]["all"].add(y[mask], prediction[mask])
                        for dimension, labels in group_values.items():
                            local_labels = labels[mask]
                            for key in np.unique(local_labels):
                                group_mask = mask & (labels == key)
                                tree[subset][dimension][str(key)].add(y[group_mask], prediction[group_mask])
                    # Retain day sufficient statistics for paired, day-cluster bootstrap.
                    for day_value in np.unique(day_values):
                        key = _date_str(day_value)
                        group_mask = day_values == day_value
                        day_sums[condition][model_name][view].setdefault(key, Score()).add(y[group_mask], prediction[group_mask])
    trip_clusters = trip_acc.result()
    trip_acc.close()
    for condition in conditions:
        for view in ("raw", "displayed"):
            for model in ("fine", "window3", "discrete"):
                tree = all_scores[condition][view][model]
                for subset in ("verified_1_2", "unresolved_0_3_4_5"):
                    for dimension, groups in tree["complete"].items():
                        for label in groups:
                            tree[subset][dimension].setdefault(label, Score())
    return (all_scores, day_sums, {date: len(ids) for date, ids in trip_ids_by_day.items()},
            cohort_status_counts, trip_clusters)


def paired_day_bootstrap(control_by_day, variant_by_day, *, seed=DAY_BOOTSTRAP_SEED,
                         replicates=DAY_BOOTSTRAP_REPLICATES, variant="weighted"):
    """Paired percentile intervals for variant-minus-control pooled day scores."""
    days = sorted(set(control_by_day["raw"]) & set(control_by_day["displayed"])
                  & set(variant_by_day["raw"]) & set(variant_by_day["displayed"]))
    if len(days) < 2:
        raise ValueError("paired day bootstrap requires at least two shared dates")
    rng = np.random.default_rng(seed)
    sample = rng.integers(0, len(days), size=(replicates, len(days)))
    output = {}
    metrics = ("within_1_min", "within_3_min", "within_5_min", "mae_scored")
    for view in ("raw", "displayed"):
        def packed(source):
            return {
                "eligible": np.asarray([source[d]["eligible"] for d in days], dtype=np.int64),
                "scored": np.asarray([source[d]["scored"] for d in days], dtype=np.int64),
                "error_sum": np.asarray([source[d]["error_sum"] for d in days], dtype=np.float64),
                "hits": {m: np.asarray([source[d]["hits"][m] for d in days], dtype=np.int64) for m in (1, 3, 5)},
            }
        c, w = packed(control_by_day[view]), packed(variant_by_day[view])
        estimates = {}
        for name in metrics:
            if name.startswith("within_"):
                minute = int(name.split("_")[1])
                point_c = c["hits"][minute].sum() / c["eligible"].sum()
                point_w = w["hits"][minute].sum() / w["eligible"].sum()
                sample_c = c["hits"][minute][sample].sum(axis=1) / c["eligible"][sample].sum(axis=1)
                sample_w = w["hits"][minute][sample].sum(axis=1) / w["eligible"][sample].sum(axis=1)
            else:
                point_c = c["error_sum"].sum() / c["scored"].sum()
                point_w = w["error_sum"].sum() / w["scored"].sum()
                sample_c = c["error_sum"][sample].sum(axis=1) / c["scored"][sample].sum(axis=1)
                sample_w = w["error_sum"][sample].sum(axis=1) / w["scored"][sample].sum(axis=1)
            delta = sample_w - sample_c
            estimates[name] = {"control": float(point_c), variant: float(point_w),
                               f"{variant}_minus_control": float(point_w - point_c),
                               "ci95": [float(np.quantile(delta, 0.025)), float(np.quantile(delta, 0.975))]}
        output[view] = estimates
    return {"method": "paired service-date cluster percentile bootstrap; same sampled dates for both conditions",
            "seed": seed, "replicates": replicates, "days": days,
            f"{variant}_minus_control": output}


def _markdown_table(rows, headers):
    if not rows:
        return "_No rows._"
    return "\n".join(["| " + " | ".join(headers) + " |",
                      "| " + " | ".join("---" for _ in headers) + " |"] +
                     ["| " + " | ".join(str(value) for value in row) + " |" for row in rows])


def build_report(result):
    if result.get("corrected_august_comparison", {}).get("status") != "verified":
        raise ValueError("cannot publish report without the verified corrected August comparison")
    def pct(value):
        return "n/a" if value is None else f"{100*value:.2f}%"
    conditions = tuple(result.get("conditions", DEFAULT_CONDITIONS))
    variants = conditions[1:]
    lines = [
        "# Recency training comparison on corrected September labels", "",
        f"This is a report-only rescore of completed model outputs. It uses the same corrected prepared evaluation rows for every condition's fits ({', '.join(conditions)}), and does not load models or run inference.", "",
        f"**Service dates:** {START_DAY} through 2026-09-19. **Eligible stop-arrivals:** {result['cohort']['rows']:,}. **Verification:** {result['verification']['status']}", "",
        "These are reused exploratory validation dates, not a fresh independent test. The input features retain present-day timetable patterns; historical timetable-as-of parity is unverified. Historical notice folds can use later records from other training months for earlier rows; this retrospective notice-fold limitation is unchanged.", "",
        f"The comparison has {3*len(conditions)} views: fine-tuned median, shared classifier ±3-minute window, and shared classifier discrete median, each under {', '.join(conditions)}. The window and discrete outputs share the same binned classifier fit within a condition, but select epochs separately using their own frozen criteria; their selected checkpoints may differ.", "",
        "## Overall metrics", "",
        "Within rates divide by every eligible arrival. MAE is over scored rows; the report also records any missing coverage.", "",
    ]
    score_rows = []
    scores = result["scores"]
    for candidate in ("fine", "window3", "discrete"):
        for condition in conditions:
            label = {"fine": "Fine median", "window3": "Distribution window ±3", "discrete": "Discrete median"}[candidate]
            for view in ("raw", "displayed"):
                row = scores[condition][candidate][view]["complete"]["overall"]["all"]
                score_rows.append([condition, label, view, row["eligible"], pct(row["within_1_min"]),
                                   pct(row["within_3_min"]), pct(row["within_5_min"]),
                                   "n/a" if row["mae_scored"] is None else f"{row['mae_scored']:.3f}", row["missing"]])
    lines.extend([_markdown_table(score_rows, ["Condition", "View", "Raw/displayed", "Rows", "Within 1m", "Within 3m", "Within 5m", "MAE min", "Missing"]), "",
                  "## Verified and unresolved corrected-label subsets", "",
                  "Verified contains status 1/2; unresolved contains status 0/3/4/5. The corrected labels are all retained in the complete-population score.", ""])
    lines.extend(["The pilot threshold audit distinguishes genuinely nonnegative corrections from labels that remain between −5 and 0: September 15 has 2,763 targets now at or above 0 and 100 still in [−5, 0); August 21 has 1,679 at or above 0 and 74 still in [−5, 0). The correction progress counter named `corrected_negative_to_nonnegative` uses the −5-minute threshold; it is not a count of targets becoming nonnegative.", "",
                  "Status codes: 0 unverified retained original; 1 source-verified original plan; 2 source-verified corrected dated plan; 3 ambiguous plan retained original; 4 unique plan without a matching post-event change timestamp retained original; 5 incomplete archive coverage retained original.", ""])
    status_rows = [[str(code), result["cohort"]["status_counts"][str(code)]] for code in range(6)]
    lines.extend(["Status counts below cover this report's September 6–19 cohort.",
                  _markdown_table(status_rows, ["Correction status", "Rows"]), ""])
    subset_rows = []
    for candidate in ("fine", "window3", "discrete"):
        for condition in conditions:
            for subset in ("verified_1_2", "unresolved_0_3_4_5"):
                for view in ("raw", "displayed"):
                    row = scores[condition][candidate][view][subset]["overall"]["all"]
                    subset_rows.append([condition, candidate, subset, view, row["eligible"], pct(row["within_5_min"]),
                                        "n/a" if row["mae_scored"] is None else f"{row['mae_scored']:.3f}"])
    lines.extend([_markdown_table(subset_rows, ["Condition", "View", "Label subset", "Raw/displayed", "Rows", "Within 5m", "MAE min"]), "",
                  "## Paired service-date bootstrap", "",
                  f"Intervals use {result['paired_day_bootstrap']['replicates']:,} paired resamples of the 14 service dates (seed {result['paired_day_bootstrap']['seed']}). Positive within-rate differences favor the condition over control; negative MAE differences favor the condition.", ""])
    boot_rows = []
    for variant in variants:
        for candidate, boot in result["paired_day_bootstrap"]["by_candidate"].items():
            for view in ("raw", "displayed"):
                for metric, values in boot[f"{variant}_minus_control"][view].items():
                    boot_rows.append([variant, candidate, view, metric, f"{values[f'{variant}_minus_control']:+.5f}",
                                      f"[{values['ci95'][0]:+.5f}, {values['ci95'][1]:+.5f}]"])
    lines.extend([_markdown_table(boot_rows, ["Condition", "View", "Raw/displayed", "Metric", "Condition − control", "95% paired day interval"]), "",
                  "## Group effects", "",
                  f"The result JSON contains complete, verified, and unresolved metrics by service date, prepared service type, source, state, and route ID, plus distinct dated-trip counts per service date. Trip-stop IDs were looked up in bounded batches against the read-only database and checked against the prepared service date. Service labels use the frozen prepared segment dictionary; {result['group_aggregation_preflight']['prepared_database_segment_mismatch_rows']:,} rows differ from the current database train.segment value and are counted in the JSON. A disk-backed preflight found {result['group_aggregation_preflight']['unique_labels_by_dimension'].get('route', 0):,} route labels and estimates {result['group_aggregation_preflight']['estimated_score_aggregate_bytes']:,} bytes for per-group scores; it refuses estimates above 1 GiB. It also reports equal-weight paired effects by dated trip; the confidence interval resamples days, not trips. Route IDs are nullable; {result['group_aggregation_preflight']['unknown_route_rows']:,} rows ({json.dumps(result['group_aggregation_preflight']['unknown_route_rows_by_source'], sort_keys=True)}) lack a route ID and remain in UNKNOWN, so route results exclude attribution for those stops.", "",
                  "### Displayed metrics by date and service type", ""])
    group_rows = []
    for dimension in ("service_date", "service"):
        for condition in conditions:
            for candidate in ("fine", "window3", "discrete"):
                groups = scores[condition][candidate]["displayed"]["complete"].get(dimension, {})
                for label, row in sorted(groups.items()):
                    group_rows.append([dimension, label, condition, candidate, row["eligible"],
                                       pct(row["within_5_min"]),
                                       "n/a" if row["mae_scored"] is None else f"{row['mae_scored']:.3f}"])
    lines.extend([_markdown_table(group_rows, ["Group", "Date/service", "Condition", "View", "Rows", "Within 5m", "MAE min"]), "",
                  "## Export, browser, and resources", "",
                  "Every candidate is gated on successful ONNX export parity and a passed actual Chromium browser check. Artifact size includes the full manifest and model file; the strict target is 35,000,000 bytes.", ""])
    export_rows = []
    for condition in conditions:
        for candidate in ("fine", "distribution", "discrete"):
            item = result["candidates"][condition][candidate]
            export_rows.append([condition, candidate, item["export"]["manifest_inclusive_download_bytes"],
                                item["export"]["strict_35mb_passed"], item["export"]["browser_passed"],
                                item["resources"].get("cgroup_peak_bytes", "n/a")])
    lines.extend([_markdown_table(export_rows, ["Condition", "Fit/view", "Manifest-inclusive bytes", "≤35 MB", "Browser pass", "Cgroup peak bytes"]), "",
                  "## Full later-window quantile diagnostics", "",
                  "These are the frozen producers’ full later-window diagnostics, not values reconstructed from point arrays. Producer scoring validates finite ordered percentile triplets; each quantile denominator must equal the eligible cohort. Discrete metrics come from full-cohort ONNX CPU replay. Point-only saved arrays cannot independently reproduce interval coverage.", ""])
    calibration_rows = []
    for condition in conditions:
        for candidate in ("fine", "distribution", "discrete"):
            diagnostics = result["candidates"][condition][candidate]["later_quantiles"]
            calibration_rows.append([condition, candidate, diagnostics["quantile_scored"],
                                     pct(diagnostics["band_coverage"]), pct(diagnostics["below_p25"]),
                                     pct(diagnostics["below_p75"])])
    lines.extend([_markdown_table(calibration_rows, ["Condition", "View", "Quantile-scored", "P25–P75 coverage", "At/below P25", "At/below P75"]), "",
                  "## Paired dated-trip effects", "",
                  "Each dated trip is one cluster; the table reports equal-cluster means over the corrected September cohort. Within-five gains are positive; MAE reductions are negative.", ""])
    trip_rows = []
    for variant in variants:
        for candidate, record in result["trip_date_clusters"].items():
            for metric, values in record["metrics"].items():
                trip_rows.append([variant, candidate, metric,
                                  f"{values['control_equal_cluster_mean']:.5f}",
                                  f"{values[f'{variant}_equal_cluster_mean']:.5f}",
                                  f"{values[f'{variant}_minus_control_mean']:+.5f}",
                                  f"{100*values[f'{variant}_clusters_improved_share']:.1f}%"])
    lines.extend([_markdown_table(trip_rows, ["Condition", "View", "Metric", "Control", "Condition", "Condition − control", "Clusters improved"]), "",
                  "## Correction-only baseline effect", "",
                  "The unchanged-checkpoint correction-only rescore is a separate analysis from refitting and recency weighting. Its old-target and corrected-target metrics and their difference are recorded in the JSON for the same September 6–19 cohort; source-verified and unresolved target subsets remain separate. This report does not attribute target-correction gains to recency.", "",
                  "The correction baseline is independently keyed to the completed correction result, prepared cache, status file, original exports, and prediction array hashes. The cohort-only corrected-minus-original differences are:", ""])
    baseline_rows = []
    for candidate in ("reference", "fine", "distribution", "discrete"):
        for view in ("raw", "displayed"):
            effects = result["correction_baseline"]["effects"][candidate][view]["correction_minus_original"]
            baseline_rows.append([candidate, view,
                                  "n/a" if effects["within_5_min"] is None else f"{effects['within_5_min']:+.5f}",
                                  "n/a" if effects["mae_scored"] is None else f"{effects['mae_scored']:+.3f}"])
    lines.extend([_markdown_table(baseline_rows, ["Unchanged output", "Raw/displayed", "Δ within 5m", "Δ MAE min"]), "",
                  "## August comparison with verified correction overlay", "",
                  "The corrected August comparison preserves the frozen August 1–31 matched population and overlays corrected labels only where source status is verified. Uncovered rows, including August 22, remain explicitly unverified original labels. This is descriptive, not a new forecast test.", ""])
    august = result["corrected_august_comparison"]
    if august["status"] == "verified":
        lines.extend([f"Supported recurring trips: {august['supported_trips']:,}; mean September 15 minus August mean: {august['mean_deviation_min']:+.2f} minutes; median: {august['median_deviation_min']:+.2f} minutes.",
                      _markdown_table([[f">{threshold} min", pct(value)] for threshold, value in
                                       ((5, august["shares"]["over_5_min"]),
                                        (15, august["shares"]["over_15_min"]),
                                        (30, august["shares"]["over_30_min"]))], ["Supported-trip deviation", "Share"]),
                      "", "The full 45-date coverage and per-date verification counts are bound in the result JSON.", ""])
    lines.extend([
                  "## Interpretation limits", "",
                  "A better reused validation score is not evidence of future generalization. The date bootstrap clusters only by service date, not recurring trip. Historical notice folds retain their cross-month look-ahead; timetable inputs are not historically reconstructed. The correction changes retrospective targets only and leaves the original forecast-time feature encoding intact. No result here changes the production pointer or authorizes promotion.", "",
                  f"Input status: {result['verification']['status']}. Resource records are copied from the completed run manifests; this summary process did not measure model inference or browser latency.", ""])
    return "\n".join(lines)


def summarize(root=ROOT, db_path=None):
    root = Path(root).resolve()
    current = root / "models/v2/CURRENT"
    current_before = sha256_file(current) if current.is_file() else None
    # Full correction verification includes both prepared columns, every committed
    # date block, archive-evidence hashes, and the frozen original source columns.
    from verify_correction_version import verify as verify_correction
    correction_result = verify_correction()
    prepared = PreparedSet(root / PREPARED.relative_to(ROOT))
    statuses, status_counts, observed_dates, correction_identity = _correction_status(prepared, root / CORRECTION.relative_to(ROOT))
    # The helper is strict about repository-local paths and completed correction provenance.
    candidate_sets, start, stop, pred_start, tail_rows, expected_days = _load_candidate_sets(root, prepared)
    if db_path is None:
        db_path = root / "data/dbrel.sqlite"
    segment_dictionary = _read_json(root / "data/ml/arrival_full/dictionary.json")["segment"]
    trip_tmp_root = root / EXPERIMENT.relative_to(ROOT) / "tmp"
    trip_tmp_root.mkdir(parents=True, exist_ok=True)
    with _frozen_source_database(root, Path(db_path)) as conn:
        group_preflight = _preflight_group_cardinality(conn, prepared, start, stop, segment_dictionary,
                                                       len(candidate_sets))
        with tempfile.TemporaryDirectory(prefix="summary-trip-date-", dir=trip_tmp_root) as scratch:
            scores, day_sums, trip_counts, cohort_status_counts, trip_clusters = _accumulate(
                candidate_sets, prepared, statuses, start, stop, pred_start, conn,
                Path(scratch) / "trip-date-clusters.sqlite", segment_dictionary)

    conditions = tuple(candidate_sets)
    score_json = {}
    day_json = {}
    for condition in conditions:
        score_json[condition] = {}
        day_json[condition] = {}
        for candidate in ("fine", "window3", "discrete"):
            score_json[condition][candidate] = {view: _metric_result(scores[condition][view][candidate])
                                                for view in ("raw", "displayed")}
            day_json[condition][candidate] = {
                view: {day: score.sufficient() for day, score in day_sums[condition][candidate][view].items()}
                for view in ("raw", "displayed")}
    paired = {}
    for candidate in ("fine", "window3", "discrete"):
        paired[candidate] = {}
        for variant in conditions[1:]:
            paired[candidate].update(paired_day_bootstrap(day_json["control"][candidate], day_json[variant][candidate],
                                                          variant=variant))
    candidate_record = {}
    for condition, tracks in candidate_sets.items():
        candidate_record[condition] = {}
        for candidate in ("fine", "distribution", "discrete"):
            data = tracks[candidate]
            candidate_record[condition][candidate] = {
                "run_key_sha256": sha256_file(EXPERIMENT / condition / "runs" / candidate / "key.json"),
                "result_sha256": sha256_file(EXPERIMENT / condition / "runs" / candidate / "result.json"),
                "prediction_array": data["array_name"],
                "prediction_sha256": data["result"]["prediction_identity"]["arrays"][data["array_name"]],
                "later_quantiles": {**data["calibration"],
                                    "quantile_scored": data["result"]["later_probe"][
                                        "median" if candidate == "discrete" else
                                        ("window3" if candidate == "distribution" else "median")]["overall"]["quantile_scored"],
                                    "source": "full frozen later-window result metric"},
                "ordered_stop_ids_sha256": data["result"]["prediction_identity"]["stop_ids_sha256"],
                "export": data["export"],
                "resources": data["result"]["resources"],
                "recency_policy": data["result"].get("recency"),
                "sampler": data["result"].get("sampler", data["key"].get("source_key", {}).get("sampler")),
                "selected_epoch": data["result"].get("selected_epoch", data["result"].get("best_epoch")),
            }
    correction_baseline = _verify_correction_baseline(
        root, prepared, statuses, start, stop, pred_start, correction_identity)
    corrected_august = _verify_corrected_august(root, correction_identity)
    pilot_threshold_path = root / CORRECTION.relative_to(ROOT) / "pilot-threshold-summary.json"
    pilot_thresholds = _read_json(pilot_threshold_path) if pilot_threshold_path.is_file() else None
    result = {
        "schema_version": 1,
        "conditions": list(conditions),
        "scope": "same corrected prepared cohort; report-only saved-array scoring; no inference/training",
        "cohort": {"start_inclusive": START_DAY, "end_exclusive": END_DAY, "rows": stop - start,
                   "service_dates": expected_days, "status_rows_seen_in_eval": len(prepared),
                   "distinct_trip_ids_by_service_date": trip_counts,
                   "status_counts": cohort_status_counts,
                   "eval_dates": observed_dates},
        "correction": correction_identity,
        "correction_baseline": correction_baseline,
        "corrected_august_comparison": corrected_august,
        "correction_version_verified": {"date_count": correction_result["dates"],
                                        "result_sha256": sha256_file(root / CORRECTION.relative_to(ROOT) / "result.json"),
                                        "pilot_threshold_summary": pilot_thresholds,
                                        "pilot_threshold_facts": {
                                            "source": "data/ml/trip_schedule_correction_20261013/pilot-threshold-summary.json",
                                            "present": pilot_thresholds is not None,
                                            "september_15_genuinely_nonnegative": 2763,
                                            "september_15_still_minus5_to_0": 100,
                                            "august_21_genuinely_nonnegative": 1679,
                                            "august_21_still_minus5_to_0": 74}},
        "candidates": candidate_record,
        "scores": score_json,
        "paired_day_bootstrap": {"by_candidate": paired, "seed": DAY_BOOTSTRAP_SEED,
                                 "replicates": DAY_BOOTSTRAP_REPLICATES,
                                 "method": "paired service-date cluster percentile bootstrap; same dates resampled for both conditions"},
        "trip_date_clusters": trip_clusters,
        "group_aggregation_preflight": group_preflight,
        "verification": {"status": "complete_with_documented_gaps", "provenance": f"all {3*len(conditions)} fit/result keys verified; ordered stop-ID companions and prediction hashes verified against corrected rows",
                          "correction": "full correction verifier passed: source, prepared columns, status/scheduled blocks and evidence hashes",
                          "browser": f"all {3*len(conditions)} exported candidate views passed actual browser runtime check",
                          "correction_baseline": "unchanged reference/original exports verified against their key, correction hash, prepared/status identity, prediction array hashes, ordered IDs, and recomputed saved metrics",
                          "corrected_august_comparison": corrected_august["status"],
                          "sqlite_id_lookup": "bounded read-only batches; trip_stop IDs and service dates verified",
                          "CURRENT_sha256_before": current_before,
                          "CURRENT_unchanged_by_summary": True},
        "limitations": ["reused validation, not independent test", "historical notice folds may use later other-month rows",
                        "timetable-as-of inputs are not reconstructed", "no paired trip-date bootstrap interval",
                        "correction-only unchanged-checkpoint score is separate from retraining and recency",
                        "no direct cause feature has certified forecast-time receipt availability"],
    }
    # Ensure the public pointer still has the same bytes immediately before publication.
    current_after = sha256_file(current) if current.is_file() else None
    if current_after != current_before:
        raise ValueError("production CURRENT changed while summary was being produced")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=SUMMARY_DIR)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    report = args.report.resolve()
    if ROOT not in output.parents or ROOT not in report.parents:
        raise SystemExit("summary outputs must remain inside this repository")
    from explore_compact_delay import guard
    cgroup = guard()
    cap = (cgroup / "memory.max").read_text().strip()
    swap = (cgroup / "memory.swap.max").read_text().strip()
    if cap == "max" or int(cap) > 2 * 2**30 or swap != "0":
        raise SystemExit("run the report inside a private cgroup with MemoryMax<=2GiB and MemorySwapMax=0")
    result = summarize()
    output.mkdir(parents=True, exist_ok=True)
    partial = output / "result.json.partial"
    partial.write_text(json.dumps(result, indent=2, allow_nan=False))
    partial.replace(output / "result.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(build_report(result))
    print(json.dumps({"result": str((output / 'result.json').relative_to(ROOT)),
                      "report": str(report.relative_to(ROOT)), "rows": result["cohort"]["rows"],
                      "verification": result["verification"]["status"]}, indent=2))


if __name__ == "__main__":
    main()
