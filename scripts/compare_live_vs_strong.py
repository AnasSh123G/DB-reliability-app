"""Head-to-head of two delay-model artifacts on the locked test week 2026-09-20..26.

Model A (live): models/v2/20261004T213106Z (CURRENT).
Model B (candidate): the v3 "strong" recency discrete artifact 20261014T091943Z.

Neither model trained on these dates. Every row is encoded for each model the
way that model expects -- its own manifest vocabulary, input prep and notice-rate
record -- through the production path (`FullHistory.encode` + `arrival_inputs`,
the same code `full_year.prepare` used for each model's own caches), and both
ONNX graphs predict exactly the same rows in the same order.

Subcommands (run each inside a memory-capped systemd unit):

  correct  -- corrected labels for the 7 test dates with the unchanged
              `build_trip_schedule_corrections.py` policy (version 2), same
              per-date logic, one role. Archive partitions already in
              data/archive are only read; a missing one is downloaded into this
              experiment's own store. Needs a <=2GiB cap (producer policy).
  predict  -- encode for A and B, check the encodings byte-for-byte against the
              caches each model was evaluated with, run onnxruntime and save the
              p25/p50/p75 outputs. Verifies A reproduces final.json's test score
              and B its recorded selection score. Needs a <=8GiB cap.
  score    -- raw and displayed metrics on original (and corrected) labels, a
              paired service-date bootstrap for B-A, results.json and the doc.

Writes only under data/ml/live_vs_strong_20261014/ and docs/live-vs-strong-results.md.
Reads data/dbrel.sqlite read-only. Never touches CURRENT or any other experiment.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from dbrel.ml.arrival import arrival_inputs  # noqa: E402
from dbrel.ml.features import CATEGORICAL  # noqa: E402
from dbrel.ml.full_history import FullHistory  # noqa: E402
from dbrel.ml.full_year import PreparedSet, RowSet, _sha, history_key  # noqa: E402
from explore_compact_delay import guard  # noqa: E402

OUT = ROOT / "data/ml/live_vs_strong_20261014"
CORR = OUT / "corrected"
PRED = OUT / "predictions"
DOC = ROOT / "docs/live-vs-strong-results.md"
DB = ROOT / "data/dbrel.sqlite"
HISTORY = ROOT / "data/ml/arrival_full"
TEST = ROOT / "data/ml/full_year/prepared/final-2026-09-26-robust-no-doy-mc20-rd56-eval"
FINAL = ROOT / "data/ml/full_year/final/2026-09-26/final.json"
A_DIR = ROOT / "models/v2/20261004T213106Z"
B_DIR = ROOT / "data/ml/recency_training_v3_20261014/strong/runs/discrete/artifacts/20261014T091943Z"
# B's own evaluation cache (search vocab/prep/notice record, corrected labels).
B_EVAL = ROOT / "data/ml/trip_schedule_correction_20261013/prepared/search-2026-09-26-robust-no-doy-mc20-rd56-eval"
B_SELECTION_END = "2026-09-06"  # exclusive; B's manifest "selection" rows
TEST_DATES = tuple(f"2026-09-{d:02d}" for d in range(20, 27))
BATCH = 65536
TOL = 1e-4
METRICS = ("within_1_min", "within_3_min", "within_5_min", "mae_scored")
MODELS = {"A": A_DIR, "B": B_DIR}
LABEL = {"A": "A live (20261004T213106Z)", "B": "B strong (20261014T091943Z)"}


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def manifest(directory):
    return json.loads((directory / "manifest.json").read_text())


def readonly_engine():
    from sqlalchemy import create_engine
    return create_engine(f"sqlite+pysqlite:///file:{DB}?mode=ro&uri=true", future=True)


def onnx_session(directory, threads):
    import onnxruntime
    opts = onnxruntime.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    return onnxruntime.InferenceSession(str(directory / "model.onnx"), opts, providers=["CPUExecutionProvider"])


def run_onnx(session, cats, nums):
    out = np.empty((len(cats), 3), dtype=np.float32)
    for s in range(0, len(cats), BATCH):
        out[s:s + BATCH] = session.run(["quantiles"], {"cat": np.ascontiguousarray(cats[s:s + BATCH], dtype=np.int64),
                                                       "num": np.ascontiguousarray(nums[s:s + BATCH], dtype=np.float32)})[0]
    return out


def basic_score(y, point):
    err = np.abs(np.asarray(y, np.float64) - np.asarray(point, np.float64))
    return {"rows": int(len(err)), "within_1_min": float((err <= 1).mean()), "within_3_min": float((err <= 3).mean()),
            "within_5_min": float((err <= 5).mean()), "mae_scored": float(err.mean())}


def compare(expected, got):
    return {name: {"expected": expected[name], "got": got[name], "abs_diff": abs(expected[name] - got[name])}
            for name in METRICS}


# ---------------------------------------------------------------------------
# predict
# ---------------------------------------------------------------------------


def predict(args):
    cg = guard()
    PRED.mkdir(parents=True, exist_ok=True)
    test = PreparedSet(TEST)
    if test.n != 2_210_206:
        raise SystemExit(f"unexpected test rows {test.n}")
    man = {k: manifest(d) for k, d in MODELS.items()}
    for k, m in man.items():
        if m["output"] != "quantiles" or m["point_rule"] != "median" or m["kind"] != "arrival-net":
            raise SystemExit(f"{k}: unexpected output contract")
    if man["A"]["features"] != man["B"]["features"]:
        raise SystemExit("feature lists differ")
    if _sha(man["A"]["vocab"]) != test.key["vocab"] or _sha(man["A"]["prep"]) != test.key["prep"]:
        raise SystemExit("test cache was not encoded with A's vocabulary/prep")
    if test.key["notice_rates"]["fingerprint"] != man["A"]["notice_rates"]["fingerprint"]:
        raise SystemExit("test cache notice rates are not A's")
    history = FullHistory(HISTORY)
    if history_key(history) != test.key["history"]:
        raise SystemExit("full history changed since the test cache was written")
    rows = RowSet(TEST_DATES)
    if rows.as_dict() != test.key["rows"]:
        raise SystemExit(f"row set differs: {rows.as_dict()} vs {test.key['rows']}")

    from dbrel.ml.notice_rates import NoticeRates
    engine = readonly_engine()
    try:
        # from_record refuses counts whose fingerprint differs from the artifact's record.
        rates = {k: NoticeRates.from_record(engine, m["notice_rates"]) for k, m in man.items()}
    finally:
        engine.dispose()
    log("notice rates loaded; fingerprints verified for A and B")
    sessions = {k: onnx_session(d, args.threads) for k, d in MODELS.items()}

    quant = {k: np.lib.format.open_memmap(PRED / f"{k}_quantiles.npy", mode="w+", dtype=np.float32,
                                          shape=(test.n, 3)) for k in MODELS}
    unknown = {k: np.zeros(len(CATEGORICAL), dtype=np.int64) for k in MODELS}
    a_cats_mismatch, a_nums_maxdiff = 0, 0.0
    n = 0
    for chunk in rows.chunks(history):
        m = len(chunk["day"])
        if not np.array_equal(chunk["stop_id"], test.column("stop_id")[n:n + m]):
            raise SystemExit("stop id order differs from the test cache")
        if not np.array_equal(chunk["day"], test.column("day")[n:n + m]):
            raise SystemExit("day order differs from the test cache")
        inputs = {}
        for k in MODELS:
            enc = history.encode(chunk, man[k]["vocab"], rates[k], fold=False)
            inputs[k] = arrival_inputs(enc, man[k]["prep"])
            unknown[k] += (inputs[k][0] == 0).sum(axis=0)
        # A's re-encoding must equal the cache final.json was scored on.
        a_cats_mismatch += int((inputs["A"][0] != test.cats[n:n + m]).sum())
        a_nums_maxdiff = max(a_nums_maxdiff, float(np.abs(inputs["A"][1] - test.nums[n:n + m]).max()))
        for k in MODELS:
            quant[k][n:n + m] = run_onnx(sessions[k], *inputs[k])
        n += m
        log(f"encoded and predicted {n:,} rows")
    if n != test.n:
        raise SystemExit(f"row count {n} != {test.n}")
    for q in quant.values():
        q.flush()
    if a_cats_mismatch or a_nums_maxdiff > 0:
        raise SystemExit(f"A re-encoding differs from test cache: cats {a_cats_mismatch}, nums {a_nums_maxdiff}")

    # Sanity A: cache inference equals re-encoded inference; score matches final.json.
    y = np.asarray(test.y, dtype=np.float64)
    a_cache = run_onnx(sessions["A"], np.asarray(test.cats), np.asarray(test.nums))
    # Batch composition differs (chunk-aligned vs whole array), so allow float-level noise.
    a_cache_maxdiff = float(np.abs(a_cache - quant["A"]).max())
    a_cache_equal = a_cache_maxdiff <= TOL
    del a_cache
    final = json.loads(FINAL.read_text())
    expected = final["scores"]["compact-winner"]["overall"]
    a_check = compare(expected, basic_score(y, quant["A"][:, 1]))
    a_pass = a_cache_equal and all(v["abs_diff"] <= TOL for v in a_check.values())
    log(f"A sanity vs final.json: {json.dumps(a_check)}; cache vs re-encoded max diff {a_cache_maxdiff}")
    if not a_pass:
        write_json(PRED / "sanity.json", {"A_vs_final_json": a_check, "A_cache_equal": a_cache_equal, "A_passed": False})
        raise SystemExit("A does not reproduce final.json; stopping")

    # Sanity B (1): re-encoding one date equals B's own evaluation cache.
    beval = PreparedSet(B_EVAL)
    if beval.key["vocab"] != _sha(man["B"]["vocab"]) or beval.key["prep"] != _sha(man["B"]["prep"]):
        raise SystemExit("B's evaluation cache is not B's vocab/prep")
    if beval.key["notice_rates"]["fingerprint"] != man["B"]["notice_rates"]["fingerprint"]:
        raise SystemExit("B's evaluation cache notice rates are not B's record")
    bday = np.asarray(beval.column("day"))
    check_day = "2026-09-19"
    epoch = int(np.datetime64(check_day, "D").astype(int))
    lo, hi = int(np.searchsorted(bday, epoch)), int(np.searchsorted(bday, epoch, side="right"))
    b_cats_mismatch, b_nums_maxdiff = 0, 0.0
    pos = lo
    for chunk in RowSet((check_day,)).chunks(history):
        m = len(chunk["day"])
        if not np.array_equal(chunk["stop_id"], beval.column("stop_id")[pos:pos + m]):
            raise SystemExit("B eval cache order differs")
        cats, nums = arrival_inputs(history.encode(chunk, man["B"]["vocab"], rates["B"], fold=False), man["B"]["prep"])
        b_cats_mismatch += int((cats != beval.cats[pos:pos + m]).sum())
        b_nums_maxdiff = max(b_nums_maxdiff, float(np.abs(nums - beval.nums[pos:pos + m]).max()))
        pos += m
    if pos != hi:
        raise SystemExit("B eval cache row count for the check date differs")
    log(f"B encoding check on {check_day} ({hi - lo:,} rows): cats mismatches {b_cats_mismatch}, nums max diff {b_nums_maxdiff}")
    if b_cats_mismatch or b_nums_maxdiff > 0:
        raise SystemExit("B re-encoding differs from B's own evaluation cache")

    # Sanity B (2): its recorded selection score on its own cache (corrected labels).
    boundary = int(np.searchsorted(bday, int(np.datetime64(B_SELECTION_END, "D").astype(int))))
    bsel = np.empty(boundary, dtype=np.float32)
    for s in range(0, boundary, 1 << 20):
        e = min(boundary, s + (1 << 20))
        bsel[s:e] = run_onnx(sessions["B"], beval.cats[s:e], beval.nums[s:e])[:, 1]
    bexp = man["B"]["scores"]["selection"]
    b_check = compare(bexp, basic_score(beval.y[0:boundary], bsel))
    b_pass = all(v["abs_diff"] <= TOL for v in b_check.values()) and boundary == bexp["eligible"]
    log(f"B sanity vs manifest selection: {json.dumps(b_check)}")

    total = test.n
    sanity = {
        "A_vs_final_json": a_check, "A_cache_equal": a_cache_equal, "A_cache_max_abs_diff": a_cache_maxdiff, "A_passed": a_pass,
        "A_reencoding_equals_test_cache": {"cats_mismatches": a_cats_mismatch, "nums_max_abs_diff": a_nums_maxdiff},
        "B_reencoding_equals_own_eval_cache": {"date": check_day, "rows": hi - lo, "cats_mismatches": b_cats_mismatch,
                                               "nums_max_abs_diff": b_nums_maxdiff},
        "B_vs_manifest_selection": b_check, "B_selection_rows": boundary, "B_passed": b_pass,
        "unknown_category_share": {k: {c: float(unknown[k][i] / total) for i, c in enumerate(CATEGORICAL)} for k in MODELS},
        "notice_rates": {k: {"first": man[k]["notice_rates"]["dates"][0], "last": man[k]["notice_rates"]["dates"][-1],
                             "n_dates": len(man[k]["notice_rates"]["dates"]),
                             "fingerprint": man[k]["notice_rates"]["fingerprint"]} for k in MODELS},
        "artifacts": {k: {"dir": str(d.relative_to(ROOT)), "model_sha256": sha_file(d / "model.onnx"),
                          "manifest_sha256": sha_file(d / "manifest.json")} for k, d in MODELS.items()},
        "predictions_sha256": {k: sha_file(PRED / f"{k}_quantiles.npy") for k in MODELS},
        "peak_memory_bytes": int((cg / "memory.peak").read_text()),
    }
    write_json(PRED / "sanity.json", sanity)
    if not b_pass:
        raise SystemExit("B does not reproduce its recorded selection score")
    log("predict complete")


# ---------------------------------------------------------------------------
# correct: build_trip_schedule_corrections.py policy v2, per-date logic unchanged,
# applied to the single test role.
# ---------------------------------------------------------------------------


def correct(args):
    import pyarrow.parquet as pq
    import build_trip_schedule_corrections as producer
    from dbrel.ingest.archive import ArchiveStore, list_day, download, _to_epoch, eva_of, covered_hours
    from dbrel.ingest.parser import parse_timetable, run_key

    cg = guard()
    if int((cg / "memory.max").read_text()) > 2 * 2**30:
        raise SystemExit("private <=2GiB zero-swap cap required (producer policy)")
    if producer.available_memory() < 7 * 2**30:
        raise SystemExit("insufficient memory headroom")
    CORR.mkdir(parents=True, exist_ok=True)
    if (CORR / "result.json").exists():
        log("corrections already complete; no writes")
        return
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.execute("PRAGMA cache_size=-32768")
    c.execute("PRAGMA temp_store=FILE")
    wal = Path(str(DB) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise SystemExit("source WAL not empty")
    c.execute("BEGIN")
    initial_version = c.execute("PRAGMA data_version").fetchone()[0]
    c.execute("SELECT id FROM trip LIMIT 1").fetchone()
    test = PreparedSet(TEST)
    n = test.n
    identity = {"database": {"bytes": DB.stat().st_size, "mtime_ns": DB.stat().st_mtime_ns},
                "metadata": {name: producer.sha(TEST / name) for name in ("meta.json", "key.json")},
                "columns": {name: producer.sha(TEST / name) for name in ("y.bin", "stop_id.bin", "day.bin")}}
    if not args.skip_db_sha:
        log("hashing database (read-only)")
        identity["database"]["sha256"] = producer.sha(DB)
    if (CORR / "source-inputs.json").exists():
        old = json.loads((CORR / "source-inputs.json").read_text())
        if (old["metadata"], old["columns"]) != (identity["metadata"], identity["columns"]) or \
                old["database"].get("sha256", identity["database"].get("sha256")) != identity["database"].get("sha256", old["database"].get("sha256")):
            raise SystemExit("source content changed; use a new directory")
    write_json(CORR / "source-inputs.json", identity)
    setup = {"policy": producer.POLICY, "producer_sha": producer.sha(Path(producer.__file__)),
             "source": str(TEST.relative_to(ROOT)),
             "dependencies": {name: producer.sha(ROOT / name) for name in ("src/dbrel/ingest/parser.py", "src/dbrel/ingest/archive.py")}}
    if (CORR / "setup.json").exists() and json.loads((CORR / "setup.json").read_text()) != setup:
        raise SystemExit("setup changed; choose a new directory")
    write_json(CORR / "setup.json", setup)

    days = np.asarray(test.column("day"))
    ids = np.asarray(test.column("stop_id"))
    oldy = np.asarray(test.y)
    if not (CORR / "y.bin").exists():
        shutil.copyfile(TEST / "y.bin", CORR / "y.bin")
    for filename, dtype in (("label_status.u8", "u1"), ("target_scheduled_utc.i64", "<i8")):
        p = CORR / filename
        if not p.exists():
            with p.open("wb") as f:
                f.truncate(n * np.dtype(dtype).itemsize)
    y = np.memmap(CORR / "y.bin", dtype="<f4", mode="r+", shape=(n,))
    status = np.memmap(CORR / "label_status.u8", dtype="u1", mode="r+", shape=(n,))
    scheduled = np.memmap(CORR / "target_scheduled_utc.i64", dtype="<i8", mode="r+", shape=(n,))

    eva_map = dict(c.execute("SELECT id,eva FROM station"))
    main_store = ArchiveStore(ROOT / "data/archive")  # read only: never saved, never downloaded into
    own_store = ArchiveStore(OUT / "archive")
    remote_cache = {}
    progress_path = CORR / "progress.json"
    stats = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    acq_path = OUT / "archive-acquisition.json"
    acquisition = json.loads(acq_path.read_text()) if acq_path.exists() else {}

    def path_of(remote):
        if main_store.is_intact(remote):
            return main_store.local_path(remote)
        if not own_store.is_intact(remote):
            if shutil.disk_usage(ROOT).free - remote.size < 35 * 2**30:
                raise SystemExit("download would breach the 35GiB disk reserve")
            log(f"downloading {remote.path} ({remote.size / 1e6:.0f} MB) into the experiment store")
            for attempt in range(6):
                try:
                    download(remote, own_store)
                    break
                except Exception as error:  # transport only; parsing happens later
                    if attempt == 5:
                        raise
                    log(f"download retry {attempt + 1}: {error}")
                    time.sleep(60 * 2**attempt)
            own_store.save()
            acquisition[remote.path] = {"bytes": remote.size, "oid": remote.oid, "store": "experiment"}
            write_json(acq_path, acquisition)
        return own_store.local_path(remote)

    q = """SELECT ts.id,t.source_run_key,ts.stop_sequence,ts.station_id,ts.supposed_arrival_utc,ts.actual_arrival_utc,
           t.source,ts.arr_delay_s FROM trip t CROSS JOIN trip_stop ts ON ts.trip_id=t.id WHERE t.service_date=?"""
    for day in TEST_DATES:
        epoch = int(np.datetime64(day, "D").astype(int))
        a, b = int(np.searchsorted(days, epoch)), int(np.searchsorted(days, epoch, side="right"))
        if day in stats:
            for name, arr in (("y", y), ("status", status), ("scheduled", scheduled)):
                if hashlib.sha256(np.asarray(arr[a:b]).tobytes()).hexdigest() != stats[day]["blocks_sha256"][name]:
                    raise SystemExit("committed correction block changed")
            continue
        y[a:b] = oldy[a:b]
        status[a:b] = 0
        scheduled[a:b] = 0
        idmap = {int(k): a + i for i, k in enumerate(ids[a:b])}
        targets = {}
        date_stats = Counter()
        started = time.monotonic()
        for sid, source_key, seq, station, old_schedule, actual, source, delay_s in c.execute(q, (day,)):
            if sid not in idmap:
                continue
            if abs(float(oldy[idmap[sid]]) - delay_s / 60) > 1e-4:
                raise SystemExit("original labels differ from the database")
            date_stats["source_prepared_rows"] += 1
            if source != "tts" or actual is None or old_schedule is None or eva_map.get(station) is None:
                continue
            key = (source_key, seq, eva_map[station])
            if key in targets:
                raise SystemExit("source stop identity ambiguous")
            targets[key] = (idmap[sid], sid, old_schedule, actual)
        if date_stats["source_prepared_rows"] != b - a:
            raise SystemExit("prepared source row missing")
        plans, matched_changes, receipt, seen_parts = {}, {}, {}, []
        target_evas = {k[2] for k in targets}
        d = date.fromisoformat(day)
        # The producer reads the mirror folders of days d-1..d+1, which under the
        # mirror's older layout held exactly calendar days d-1..d+1. From mid-September
        # the mirror files a day's late (20-23h) and early (00-03h) hours under the
        # neighbouring folder, so the same calendar window now spans folders d-2..d+2.
        # Keep exactly the partitions whose declared hours lie on calendar days d-1..d+1.
        window = {d + timedelta(days=k) for k in (-1, 0, 1)}
        all_parts = []
        for edge in (-2, -1, 0, 1, 2):
            fetchday = d + timedelta(days=edge)
            if fetchday not in remote_cache:
                remote_cache[fetchday] = list_day(fetchday)
            for r in remote_cache[fetchday]:
                hours = covered_hours([r.path])
                if hours and all(x in window for x, _ in hours):
                    all_parts.append((r, path_of(r)))
        covered = covered_hours([p for _, p in all_parts])
        required = {(d, h) for h in range(24)} | {(d + timedelta(days=1), h) for h in range(24)} | \
                   {(d - timedelta(days=1), h) for h in range(20, 24)}
        missing = sorted((str(x), h) for x, h in required - covered)
        if missing:
            status[a:b] = 5
            date_stats["incomplete_archive_rows"] = b - a
            log(f"{day} missing declared archive coverage {missing}")
        for remote, path in ([] if missing else all_parts):
            st = path.stat()
            part_sha = producer.sha(path)
            seen_parts.append({"path": str(path.relative_to(ROOT)), "sha256": part_sha, "bytes": st.st_size})
            if len(remote.oid) == 64 and part_sha != remote.oid:
                raise SystemExit("archive content differs from upstream SHA256")
            for batch in pq.ParquetFile(path).iter_batches(batch_size=16, columns=["timestamp", "url", "api_name", "response_data", "status_code"]):
                for row in batch.to_pylist():
                    if str(row["status_code"]) != "200":
                        continue
                    eva = eva_of(row["url"] or "")
                    if eva not in target_evas:
                        continue
                    body = row["response_data"]
                    recorded = _to_epoch(row["timestamp"])
                    if not body or recorded is None:
                        continue
                    raw = body.encode() if isinstance(body, str) else body
                    parsed = parse_timetable(raw)
                    for stop in parsed.stops:
                        key = (run_key(stop.stop_id), stop.stop_index, eva)
                        if key not in targets or not stop.arrival:
                            continue
                        if stop.eva is not None and stop.eva != eva:
                            raise SystemExit("payload station mismatch")
                        event = stop.arrival
                        if event.planned_ts is not None and str(row["api_name"]).endswith("/plan"):
                            plans.setdefault(key, set()).add(event.planned_ts)
                            signature = (key, event.planned_ts)
                            if signature not in receipt or recorded < receipt[signature]["recorded_at"]:
                                receipt[signature] = {"recorded_at": recorded, "response_sha256": hashlib.sha256(raw).hexdigest(),
                                                      "partition": str(path.relative_to(ROOT))}
                        if str(row["api_name"]).endswith("/fchg") and event.changed_ts == targets[key][3] and recorded >= event.changed_ts + 300:
                            if key not in matched_changes or recorded < matched_changes[key]["changed_recorded_at"]:
                                matched_changes[key] = {"changed_recorded_at": recorded, "changed_response_sha256": hashlib.sha256(raw).hexdigest(),
                                                        "changed_partition": str(path.relative_to(ROOT)), "changed_endpoint": str(row["api_name"])}
            if path.stat().st_size != st.st_size or producer.sha(path) != part_sha:
                raise SystemExit("archive changed during read")
        evidence = []
        for key, (index, sid, old_schedule, actual) in ([] if missing else targets.items()):
            pp = plans.get(key, set())
            date_stats["target_tts_rows"] += 1
            if not pp:
                date_stats["missing_plan"] += 1
                continue
            if len(pp) != 1:
                status[index] = 3
                date_stats["ambiguous_plan"] += 1
                continue
            if key not in matched_changes:
                status[index] = 4
                date_stats["raw_changed_not_matched"] += 1
                continue
            pt = next(iter(pp))
            new = (actual - pt) / 60
            if not np.isfinite(new):
                raise SystemExit("nonfinite correction")
            scheduled[index] = pt
            y[index] = new
            status[index] = 1 if pt == old_schedule else 2
            date_stats["source_verified"] += 1
            date_stats["corrected"] += pt != old_schedule
            date_stats["corrected_negative_to_nonnegative"] += float(oldy[index]) < -5 and new >= -5
            date_stats["corrected_outside_previous_120min_bound"] += abs(new) > 120
            rec = receipt[(key, pt)]
            date_stats["plan_seen_before_corrected_5h_cutoff"] += rec["recorded_at"] <= pt - 18000
            if pt != old_schedule:
                evidence.append({"date": day, "prepared_row": index, "stop_id": sid, "source_run_key": key[0],
                                 "stop_sequence": key[1], "eva": key[2], "original_target_min": float(oldy[index]),
                                 "corrected_target_min": new, "original_schedule_utc": old_schedule, "dated_plan_utc": pt,
                                 "actual_utc": actual, **rec, **matched_changes[key]})
        for arr in (y, status, scheduled):
            arr.flush()
        proof_dir = CORR / "evidence"
        proof_dir.mkdir(exist_ok=True)
        proof = proof_dir / f"{day}.csv"
        tmp = proof.with_suffix(".csv.tmp")
        with tmp.open("w", newline="") as f:
            if evidence:
                w = csv.DictWriter(f, fieldnames=list(evidence[0]))
                w.writeheader()
                w.writerows(evidence)
        tmp.replace(proof)
        stats[day] = {"rows": b - a, "counts": {k: int(v) for k, v in date_stats.items()}, "archive_inputs": seen_parts,
                      "missing_coverage": missing, "evidence_sha256": producer.sha(proof),
                      "blocks_sha256": {name: hashlib.sha256(np.asarray(arr[a:b]).tobytes()).hexdigest()
                                        for name, arr in (("y", y), ("status", status), ("scheduled", scheduled))},
                      "seconds": time.monotonic() - started}
        write_json(progress_path, stats)
        log(f"{day} {json.dumps(stats[day]['counts'])} seconds {stats[day]['seconds']:.1f}")
        del idmap, targets, plans, matched_changes, receipt, evidence
        remote_cache = {k: v for k, v in remote_cache.items() if k >= d - timedelta(days=1)}
        stats[day]["mirror_window"] = "calendar days d-1..d+1 selected from mirror folders d-2..d+2"
        write_json(progress_path, stats)
    c.rollback()
    if c.execute("PRAGMA data_version").fetchone()[0] != initial_version:
        raise SystemExit("database changed during the correction snapshot")
    c.close()
    write_json(CORR / "result.json", {"dates": list(stats), "policy": producer.POLICY, "rows": n,
                                      "columns": {p.name: producer.sha(p) for p in (CORR / "y.bin", CORR / "label_status.u8", CORR / "target_scheduled_utc.i64")},
                                      "progress_sha": producer.sha(progress_path),
                                      "peak_memory_bytes": int((cg / "memory.peak").read_text())})
    log("corrections complete")


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


def score(args):
    from summarize_recency_training import (Score, paired_day_bootstrap, shown, DAY_BOOTSTRAP_SEED,
                                            DAY_BOOTSTRAP_REPLICATES)
    if DAY_BOOTSTRAP_SEED != 4231 or DAY_BOOTSTRAP_REPLICATES != 2000:
        raise SystemExit("summarizer bootstrap constants changed")
    test = PreparedSet(TEST)
    sanity = json.loads((PRED / "sanity.json").read_text())
    if not (sanity["A_passed"] and sanity["B_passed"]):
        raise SystemExit("sanity checks did not pass")
    for k in MODELS:
        if sha_file(PRED / f"{k}_quantiles.npy") != sanity["predictions_sha256"][k]:
            raise SystemExit("prediction file changed")
    pct = {k: np.load(PRED / f"{k}_quantiles.npy") for k in MODELS}
    points = {k: pct[k][:, 1].astype(np.float64) for k in MODELS}
    day = np.asarray(test.column("day"))
    labels = {"original": np.asarray(test.y, dtype=np.float64)}
    corr_status = None
    if (CORR / "result.json").exists():
        res = json.loads((CORR / "result.json").read_text())
        if sha_file(CORR / "y.bin") != res["columns"]["y.bin"]:
            raise SystemExit("corrected labels changed since completion")
        labels["corrected"] = np.fromfile(CORR / "y.bin", dtype="<f4").astype(np.float64)
        corr_status = np.fromfile(CORR / "label_status.u8", dtype="u1")
        if len(labels["corrected"]) != test.n:
            raise SystemExit("corrected label count differs")
    results = {"rows": int(test.n), "dates": [str(np.datetime64(int(d), "D")) for d in np.unique(day)],
               "label_sets": {}, "sanity": sanity}
    for label_name, y in labels.items():
        subsets = {} if corr_status is None else {"label_changed": corr_status == 2, "label_unchanged": corr_status != 2}
        entry = {"overall": {}, "by_day": {}, "subsets": {}}
        day_sums = {k: {"raw": {}, "displayed": {}} for k in MODELS}
        for k in MODELS:
            for view in ("raw", "displayed"):
                pred = points[k] if view == "raw" else shown(points[k])
                s = Score()
                s.add(y, pred)
                entry["overall"].setdefault(k, {})[view] = s.result()
                for dv in np.unique(day):
                    mask = day == dv
                    ds = Score()
                    ds.add(y[mask], pred[mask])
                    key = str(np.datetime64(int(dv), "D"))
                    day_sums[k][view][key] = ds.sufficient()
                    entry["by_day"].setdefault(key, {}).setdefault(k, {})[view] = ds.result()
                for sub, mask in subsets.items():
                    ss = Score()
                    ss.add(y[mask], pred[mask])
                    entry["subsets"].setdefault(sub, {}).setdefault(k, {})[view] = ss.result()
            band = (y >= pct[k][:, 0]) & (y <= pct[k][:, 2])
            entry["overall"][k]["band_coverage_p25_p75"] = float(band.mean())
        entry["bootstrap"] = paired_day_bootstrap(day_sums["A"], day_sums["B"], variant="B")
        results["label_sets"][label_name] = entry
    if corr_status is not None:
        values, counts = np.unique(corr_status, return_counts=True)
        results["correction_status_counts"] = {str(int(s)): int(c) for s, c in zip(values, counts)}
        results["correction_progress"] = {d: v["counts"] for d, v in json.loads((CORR / "progress.json").read_text()).items()}
        results["corrected_label_rows_changed"] = int((labels["corrected"] != labels["original"]).sum())
    write_json(OUT / "results.json", results)
    DOC.write_text(build_doc(results))
    log(f"wrote {OUT / 'results.json'} and {DOC}")


def build_doc(r):
    def pct(v):
        return f"{100 * v:.2f}%"

    def pp(v):
        return f"{100 * v:+.2f} pp"

    def excludes_zero(ci):
        return ci[0] > 0 or ci[1] < 0

    sets = r["label_sets"]
    lines = ["# Live vs strong: delay-model head-to-head on the locked test week", "", "## Summary", ""]
    for name, e in sets.items():
        bd = e["bootstrap"]["B_minus_control"]["displayed"]
        w5, w1, mae = bd["within_5_min"], bd["within_1_min"], bd["mae_scored"]
        winner = "B (strong)" if w5["B_minus_control"] > 0 else "A (live)"
        sig = "95% day interval excludes zero" if excludes_zero(w5["ci95"]) else "95% day interval includes zero"
        lines.append(
            f"- **{name.capitalize()} labels, as displayed in the app:** {winner} is right to within 5 minutes more often — "
            f"A {pct(w5['control'])} vs B {pct(w5['B'])} ({pp(w5['B_minus_control'])}, {sig}). "
            f"Within 1 minute: A {pct(w1['control'])} vs B {pct(w1['B'])} ({pp(w1['B_minus_control'])}). "
            f"Average error: A {mae['control']:.2f} vs B {mae['B']:.2f} min ({mae['B_minus_control']:+.3f}).")
    if "corrected" in sets:
        same = sets["corrected"]["subsets"]["label_unchanged"]
        changed = sets["corrected"]["subsets"]["label_changed"]
        a, b = same["A"]["displayed"], same["B"]["displayed"]
        lines.append(
            f"- **Where the gap comes from:** on the {a['eligible']:,} arrivals whose label is identical in both sets "
            f"({pct(a['eligible'] / r['rows'])}), the models are close — within 5 min A {pct(a['within_5_min'])} vs B "
            f"{pct(b['within_5_min'])}, MAE A {a['mae_scored']:.2f} vs B {b['mae_scored']:.2f}. Almost the whole difference "
            f"is the {changed['A']['displayed']['eligible']:,} arrivals the correction re-references to the dated plan: "
            "each model is far better on the label definition it was trained on, so which model 'wins' depends mainly "
            "on which label definition is taken as the truth.")
    lines += ["",
              "Both models predicted the same 2,210,206 arrivals of 2026-09-20..26, a week neither trained on. "
              "A is the live model (`models/v2/20261004T213106Z`, trained 2025-11-03..2026-09-19 on uncorrected labels). "
              "B is the v3 strong recency candidate (`data/ml/recency_training_v3_20261014/strong/runs/discrete/artifacts/20261014T091943Z`, "
              "trained 2025-11-03..2026-08-22 on corrected labels with 3x recency weighting — four fewer weeks of history than A). "
              "The original labels favour A, which learned the same uncorrected label definition; corrected labels follow "
              "the definition B was trained on.", "",
              "'Displayed' applies the app's rule (`shown` in `scripts/summarize_recency_training.py`): an estimate below 1 "
              "minute shows as 0, otherwise it rounds half up to a whole minute. B already outputs whole-minute quantiles, so "
              "its raw and displayed points differ only where it predicts negative minutes.", ""]
    for name, e in sets.items():
        lines += [f"## Scores on {name} labels", "",
                  "| Model | View | Within 1 min | Within 3 min | Within 5 min | MAE (min) |",
                  "|---|---|---:|---:|---:|---:|"]
        for k in MODELS:
            for view in ("raw", "displayed"):
                s = e["overall"][k][view]
                lines.append(f"| {LABEL[k]} | {view} | {pct(s['within_1_min'])} | {pct(s['within_3_min'])} | "
                             f"{pct(s['within_5_min'])} | {s['mae_scored']:.4f} |")
        lines += ["", f"Share of arrivals inside the p25–p75 band (ideal 50%): A {pct(e['overall']['A']['band_coverage_p25_p75'])}, "
                      f"B {pct(e['overall']['B']['band_coverage_p25_p75'])}.", "",
                  f"### B − A, paired service-date bootstrap ({name} labels)", "",
                  "2,000 paired resamples of the 7 service dates (seed 4231), percentile 95% interval, as in "
                  "`summarize_recency_training.paired_day_bootstrap`. Positive within-rate differences and negative MAE "
                  "differences favour B.", "",
                  "| View | Metric | A | B | B − A | 95% interval |", "|---|---|---:|---:|---:|---|"]
        for view in ("raw", "displayed"):
            for metric, est in e["bootstrap"]["B_minus_control"][view].items():
                if metric == "mae_scored":
                    lines.append(f"| {view} | MAE (min) | {est['control']:.4f} | {est['B']:.4f} | {est['B_minus_control']:+.4f} | "
                                 f"[{est['ci95'][0]:+.4f}, {est['ci95'][1]:+.4f}] |")
                else:
                    lines.append(f"| {view} | {metric.replace('_min', ' min').replace('_', ' ')} | {pct(est['control'])} | "
                                 f"{pct(est['B'])} | {pp(est['B_minus_control'])} | [{pp(est['ci95'][0])}, {pp(est['ci95'][1])}] |")
        lines += ["", f"### By service date ({name} labels, displayed)", "",
                  "| Date | Rows | A within 5 | B within 5 | A within 1 | B within 1 | A MAE | B MAE |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for d, v in e["by_day"].items():
            a, b = v["A"]["displayed"], v["B"]["displayed"]
            lines.append(f"| {d} | {a['eligible']:,} | {pct(a['within_5_min'])} | {pct(b['within_5_min'])} | "
                         f"{pct(a['within_1_min'])} | {pct(b['within_1_min'])} | {a['mae_scored']:.3f} | {b['mae_scored']:.3f} |")
        titles = {"label_changed": "Only the arrivals whose label the correction changed (status 2)",
                  "label_unchanged": "Only the arrivals whose label is the same in both label sets"}
        for sub, v in e["subsets"].items():
            lines += ["", f"### {titles[sub]}, {name} labels", "",
                      "| Model | View | Rows | Within 1 min | Within 5 min | MAE (min) |", "|---|---|---:|---:|---:|---:|"]
            for k in MODELS:
                for view in ("raw", "displayed"):
                    s = v[k][view]
                    lines.append(f"| {LABEL[k]} | {view} | {s['eligible']:,} | {pct(s['within_1_min'])} | "
                                 f"{pct(s['within_5_min'])} | {s['mae_scored']:.3f} |")
        lines.append("")
    s = r["sanity"]
    unk = ", ".join(f"{c} {pct(s['unknown_category_share']['A'][c])} / {pct(s['unknown_category_share']['B'][c])}"
                    for c in CATEGORICAL if s["unknown_category_share"]["A"][c] or s["unknown_category_share"]["B"][c])
    lines += ["## How the comparison was made", "",
              "- Rows: the locked test cache `data/ml/full_year/prepared/final-2026-09-26-robust-no-doy-mc20-rd56-eval`, "
              "re-read from the full-history shards (`data/ml/arrival_full`) in the same order; stop IDs and days checked row by row.",
              "- Encoding: every row was encoded separately for each model through the production path "
              "(`FullHistory.encode` + `arrival_inputs`, what `full_year.prepare` uses) with that model's own manifest "
              "vocabulary, input prep (robust centre/scale) and notice-rate record (`NoticeRates.from_record` over "
              "`notice_rate_daily`, fingerprint-checked, read-only database): "
              f"A {s['notice_rates']['A']['n_dates']} dates {s['notice_rates']['A']['first']}..{s['notice_rates']['A']['last']}, "
              f"B {s['notice_rates']['B']['n_dates']} dates {s['notice_rates']['B']['first']}..{s['notice_rates']['B']['last']}. "
              "A category outside a model's vocabulary goes to that model's unknown slot 0. Inference: onnxruntime (CPU) on "
              "each artifact's `model.onnx`; the point estimate is the p50 output for both.",
              "- Check on A: its re-encoded inputs equal the test cache byte for byte, and its score reproduces final.json "
              f"(differences: within 1 {s['A_vs_final_json']['within_1_min']['abs_diff']:.1e}, within 5 "
              f"{s['A_vs_final_json']['within_5_min']['abs_diff']:.1e}, MAE {s['A_vs_final_json']['mae_scored']['abs_diff']:.1e}).",
              f"- Check on B: re-encoding {s['B_reencoding_equals_own_eval_cache']['date']} "
              f"({s['B_reencoding_equals_own_eval_cache']['rows']:,} rows) equals B's own evaluation cache byte for byte, and B "
              f"reproduces its manifest selection score on that cache ({s['B_selection_rows']:,} rows; within 5 difference "
              f"{s['B_vs_manifest_selection']['within_5_min']['abs_diff']:.1e}, MAE difference "
              f"{s['B_vs_manifest_selection']['mae_scored']['abs_diff']:.1e}).",
              f"- Share of test arrivals with an unknown category (A / B): {unk}."]
    if "correction_status_counts" in r:
        names = {"0": "unverified", "1": "verified, plan unchanged", "2": "corrected to the dated plan", "3": "ambiguous plan",
                 "4": "no matching change record", "5": "incomplete archive"}
        lines.append("- Corrected labels: the `scripts/build_trip_schedule_corrections.py` policy (version 2), same per-date "
                     "logic, applied to the 7 test dates; output in `data/ml/live_vs_strong_20261014/corrected/`. Status counts: "
                     + ", ".join(f"{names[k]} {v:,}" for k, v in r["correction_status_counts"].items())
                     + f". Labels changed on {r['corrected_label_rows_changed']:,} arrivals. One mechanical adaptation: "
                     "the producer reads mirror folders d-1..d+1, which under the mirror's older layout held exactly calendar "
                     "days d-1..d+1; from mid-September the mirror files a day's late/early hours in the neighbouring folder, "
                     "so the producer's own coverage check would have marked 2026-09-20 incomplete (status 5, all labels "
                     "retained). This run selects the partitions whose declared hours lie on calendar days d-1..d+1 from "
                     "folders d-2..d+2, i.e. the same evidence window. Partitions already in `data/archive` were only read; "
                     "the 9 missing ones (2026-09-26 13h to 2026-09-27 23h) were downloaded into "
                     "`data/ml/live_vs_strong_20261014/archive/`.")
    else:
        lines.append("- Corrected labels were not produced; only original labels are reported.")
    lines += ["", "## Caveats", "",
              "- Seven service dates only. The bootstrap clusters by date, so it cannot reflect trip-level dependence or "
              "differences between weeks; with 7 clusters the intervals are rough.",
              "- B's training window ends four weeks before A's (2026-08-22 vs 2026-09-19). Part of any gap is data recency, "
              "not label correction or recency weighting.",
              "- Original labels favour A (the label definition it trained on); corrected labels favour B's definition.",
              "- B's quantiles are whole minutes (discrete CDF head), A's are continuous. That alone explains most of the "
              "raw within-1-minute gap; the displayed view, which rounds both, is the fair comparison for what users see.",
              "- Historical five-hour-cutoff replay with present-day timetable patterns, not a logged prospective forecast.",
              "- This report changes nothing in production: CURRENT, the deployed database and other experiments are untouched.", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("predict")
    p.add_argument("--threads", type=int, default=8)
    p = sub.add_parser("correct")
    p.add_argument("--skip-db-sha", action="store_true")
    sub.add_parser("score")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    {"predict": predict, "correct": correct, "score": score}[args.command](args)


if __name__ == "__main__":
    main()
