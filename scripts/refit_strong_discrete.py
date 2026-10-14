"""Refit the "strong discrete" delay model on all data through 2026-09-19 with corrected labels.

Model under refit: the binned-classifier "distribution" track of
`train_recency_delay.py` (configuration('distribution'): seed 1, 12 epochs, from
scratch), strong recency weights (amplitude 2.0 over the last calendar month,
fixed full-dataset mean normalization, permuted date-block pools), no epoch
selection (epoch 12 is used), then exported with the discrete-CDF postprocessing
of `check_recency_discrete_delay.py`.

Rows and inputs are exactly the live model A's final fit set
(data/ml/full_year/prepared/final-2026-09-26-robust-no-doy-mc20-rd56-fit, A's
vocabulary, input prep and notice-rate record); only the target is replaced by
the version-2 dated-plan corrected label.

Subcommands, each run inside a memory-capped systemd unit:

  correct  -- corrected labels for the final fit rows of 2026-08-21 and 2026-08-22
              (the rows the search fit/eval correction never covered), with the
              unchanged build_trip_schedule_corrections.py policy and the d-2..d+2
              mirror-folder selection of compare_live_vs_strong.py. <=2GiB cap.
  labels   -- assemble the corrected target for every final fit row: the search
              correction where the row exists there (joined on service date + stop
              ID), this run's correction otherwise; fails on any uncovered row or on
              any disagreement where both corrections exist.
  train    -- GPU training (torch ROCm venv).
  export   -- distribution ONNX (probabilities) + discrete ONNX (quantiles), parity
              checks, browser fixtures, deployable artifact.
  predict  -- A and new B on the locked test week 2026-09-20..26 (earlier B's
              predictions are reused from data/ml/live_vs_strong_20261014).
  score    -- raw/displayed metrics on original and corrected labels, paired
              service-date bootstrap, results.json and docs/live-vs-refit-results.md.

The pinned recency scripts are imported, never edited. Writes only under
data/ml/refit_strong_20261014/ (and the doc). data/dbrel.sqlite is read-only.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
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

from dbrel.ml.full_year import PreparedSet, _sha  # noqa: E402
from explore_compact_delay import guard  # noqa: E402

OUT = ROOT / "data/ml/refit_strong_20261014"
CORR = OUT / "corrected"
PREPARED = OUT / "prepared/final-2026-09-26-robust-no-doy-mc20-rd56-fit-corrected"
RUN = OUT / "runs/distribution"
DISCRETE = OUT / "runs/discrete"
ARTIFACTS = OUT / "artifacts"
PRED = OUT / "predictions"
DOC = ROOT / "docs/live-vs-refit-results.md"
DB = ROOT / "data/dbrel.sqlite"

FINAL_FIT = ROOT / "data/ml/full_year/prepared/final-2026-09-26-robust-no-doy-mc20-rd56-fit"
TEST = ROOT / "data/ml/full_year/prepared/final-2026-09-26-robust-no-doy-mc20-rd56-eval"
SEARCH = ROOT / "data/ml/trip_schedule_correction_20261013"
SEARCH_SETS = [SEARCH / f"prepared/search-2026-09-26-robust-no-doy-mc20-rd56-{r}" for r in ("fit", "eval")]
A_DIR = ROOT / "models/v2/20261004T213106Z"
B0_DIR = ROOT / "data/ml/recency_training_v3_20261014/strong/runs/discrete/artifacts/20261014T091943Z"
STRONG = ROOT / "data/ml/recency_training_v3_20261014/strong/runs"
LVS = ROOT / "data/ml/live_vs_strong_20261014"
FINAL_JSON = ROOT / "data/ml/full_year/final/2026-09-26/final.json"

CORRECTION_DATES = ("2026-08-21", "2026-08-22")
POLICY_END = "2026-09-19"
AMPLITUDE = 2.0
MONITOR_ROWS = 200_000  # in-sample per-epoch monitor only; never used for selection
EPOCH = 12
PROB_TOL = 1e-4


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
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(f.fileno(), f.tell() - len(b), len(b), os.POSIX_FADV_DONTNEED)
    return h.hexdigest()


def epoch_day(text):
    return int(np.datetime64(text, "D").astype(int))


def day_str(value):
    return str(np.datetime64(int(value), "D"))


def manifest(directory):
    return json.loads((directory / "manifest.json").read_text())


def day_range(day_column, value):
    """[a, b) rows of one service date in a date-ordered day column."""
    return int(np.searchsorted(day_column, value)), int(np.searchsorted(day_column, value, side="right"))


# ---------------------------------------------------------------------------
# correct: build_trip_schedule_corrections.py policy v2, per-date logic unchanged,
# applied to the final fit rows of 2026-08-21 and 2026-08-22.
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
    fit = PreparedSet(FINAL_FIT)
    n = fit.n
    days = np.memmap(FINAL_FIT / "day.bin", dtype="<i4", mode="r", shape=(n,))
    ranges = {d: day_range(days, epoch_day(d)) for d in CORRECTION_DATES}
    base, end = ranges[CORRECTION_DATES[0]][0], ranges[CORRECTION_DATES[-1]][1]
    if any(b <= a for a, b in ranges.values()) or ranges[CORRECTION_DATES[0]][1] != ranges[CORRECTION_DATES[1]][0]:
        raise SystemExit("correction dates must be present and adjacent in the final fit set")
    m = end - base
    ids = np.array(np.memmap(FINAL_FIT / "stop_id.bin", dtype="<i8", mode="r", shape=(n,))[base:end])
    oldy = np.array(np.memmap(FINAL_FIT / "y.bin", dtype="<f4", mode="r", shape=(n,))[base:end])

    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.execute("PRAGMA cache_size=-32768")
    c.execute("PRAGMA temp_store=FILE")
    wal = Path(str(DB) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise SystemExit("source WAL not empty")
    c.execute("BEGIN")
    initial_version = c.execute("PRAGMA data_version").fetchone()[0]
    c.execute("SELECT id FROM trip LIMIT 1").fetchone()
    identity = {"database": {"bytes": DB.stat().st_size, "mtime_ns": DB.stat().st_mtime_ns},
                "metadata": {name: producer.sha(FINAL_FIT / name) for name in ("meta.json", "key.json")},
                "rows": {"base": base, "end": end,
                         "ranges": {d: list(r) for d, r in ranges.items()},
                         "stop_id_sha256": hashlib.sha256(ids.tobytes()).hexdigest(),
                         "y_sha256": hashlib.sha256(oldy.tobytes()).hexdigest()}}
    if (CORR / "source-inputs.json").exists():
        old = json.loads((CORR / "source-inputs.json").read_text())
        if (old["metadata"], old["rows"]) != (identity["metadata"], identity["rows"]):
            raise SystemExit("source content changed; use a new directory")
    write_json(CORR / "source-inputs.json", identity)
    setup = {"policy": producer.POLICY, "producer_sha": producer.sha(Path(producer.__file__)),
             "source": str(FINAL_FIT.relative_to(ROOT)), "dates": list(CORRECTION_DATES),
             "mirror_window": "calendar days d-1..d+1 selected from mirror folders d-2..d+2 (compare_live_vs_strong.py)",
             "dependencies": {name: producer.sha(ROOT / name) for name in ("src/dbrel/ingest/parser.py", "src/dbrel/ingest/archive.py")}}
    if (CORR / "setup.json").exists() and json.loads((CORR / "setup.json").read_text()) != setup:
        raise SystemExit("setup changed; choose a new directory")
    write_json(CORR / "setup.json", setup)

    for filename, dtype in (("y.f32", "<f4"), ("label_status.u8", "u1"), ("target_scheduled_utc.i64", "<i8")):
        p = CORR / filename
        if not p.exists():
            with p.open("wb") as f:
                f.truncate(m * np.dtype(dtype).itemsize)
    y = np.memmap(CORR / "y.f32", dtype="<f4", mode="r+", shape=(m,))
    status = np.memmap(CORR / "label_status.u8", dtype="u1", mode="r+", shape=(m,))
    scheduled = np.memmap(CORR / "target_scheduled_utc.i64", dtype="<i8", mode="r+", shape=(m,))

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
    for day in CORRECTION_DATES:
        a, b = (x - base for x in ranges[day])
        if day in stats:
            for name, arr in (("y", y), ("status", status), ("scheduled", scheduled)):
                if hashlib.sha256(np.asarray(arr[a:b]).tobytes()).hexdigest() != stats[day]["blocks_sha256"][name]:
                    raise SystemExit("committed correction block changed")
            continue
        y[a:b] = oldy[a:b]
        status[a:b] = 0
        scheduled[a:b] = 0
        idmap = {int(k): a + i for i, k in enumerate(ids[a:b])}
        if len(idmap) != b - a:
            raise SystemExit("duplicate stop IDs within a date")
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
                evidence.append({"date": day, "prepared_row": base + index, "stop_id": sid, "source_run_key": key[0],
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
                      "seconds": time.monotonic() - started,
                      "mirror_window": "calendar days d-1..d+1 selected from mirror folders d-2..d+2"}
        write_json(progress_path, stats)
        log(f"{day} {json.dumps(stats[day]['counts'])} seconds {stats[day]['seconds']:.1f}")
        del idmap, targets, plans, matched_changes, receipt, evidence
        remote_cache = {k: v for k, v in remote_cache.items() if k >= d - timedelta(days=1)}
    c.rollback()
    if c.execute("PRAGMA data_version").fetchone()[0] != initial_version:
        raise SystemExit("database changed during the correction snapshot")
    c.close()
    write_json(CORR / "result.json", {"dates": list(stats), "policy": producer.POLICY, "rows": m, "base_row": base,
                                      "columns": {p.name: producer.sha(p) for p in (CORR / "y.f32", CORR / "label_status.u8", CORR / "target_scheduled_utc.i64")},
                                      "progress_sha": producer.sha(progress_path),
                                      "peak_memory_bytes": int((cg / "memory.peak").read_text())})
    log("corrections complete")


# ---------------------------------------------------------------------------
# labels: corrected target for every final fit row
# ---------------------------------------------------------------------------


class Concat:
    """Row-concatenated view of one column across the search fit and eval sets."""

    def __init__(self, name, dtype):
        self.parts = [np.memmap(d / name, dtype=dtype, mode="r") for d in SEARCH_SETS]
        self.split = len(self.parts[0])

    def __len__(self):
        return self.split + len(self.parts[1])

    def take(self, a, b):
        out = []
        if a < self.split:
            out.append(np.array(self.parts[0][a:min(b, self.split)]))
        if b > self.split:
            out.append(np.array(self.parts[1][max(a, self.split) - self.split:b - self.split]))
        return np.concatenate(out) if out else np.zeros(0, dtype=self.parts[0].dtype)


def labels(args):
    import build_trip_schedule_corrections as producer
    cg = guard()
    corr = json.loads((CORR / "result.json").read_text())
    for name, expected in corr["columns"].items():
        if producer.sha(CORR / name) != expected:
            raise SystemExit(f"this run's correction changed: {name}")
    search_keys = [json.loads((d / "key.json").read_text()) for d in SEARCH_SETS]
    for d, k in zip(SEARCH_SETS, search_keys):
        if k["label_correction"]["status"] != "complete":
            raise SystemExit("search correction incomplete")
        for name in ("y.bin", "stop_id.bin", "day.bin"):
            if producer.sha(d / name) != k["label_correction"]["columns"][name]["sha256"]:
                raise SystemExit(f"search correction column changed: {d.name}/{name}")
    fit = PreparedSet(FINAL_FIT)
    n = fit.n
    fday = np.fromfile(FINAL_FIT / "day.bin", dtype="<i4")
    if np.any(np.diff(fday) < 0):
        raise SystemExit("final fit set not ordered by service date")
    fid = np.memmap(FINAL_FIT / "stop_id.bin", dtype="<i8", mode="r")
    fy = np.memmap(FINAL_FIT / "y.bin", dtype="<f4", mode="r")
    sday = Concat("day.bin", "<i4")
    sday_all = sday.take(0, len(sday))
    if np.any(np.diff(sday_all) < 0):
        raise SystemExit("search sets not ordered by service date")
    sid, sy, sst = Concat("stop_id.bin", "<i8"), Concat("y.bin", "<f4"), Concat("label_status.u8", "u1")
    base = corr["base_row"]
    cy = np.memmap(CORR / "y.f32", dtype="<f4", mode="r")
    cst = np.memmap(CORR / "label_status.u8", dtype="u1", mode="r")
    corr_dates = {epoch_day(d) for d in corr["dates"]}

    PREPARED.mkdir(parents=True, exist_ok=True)
    for p in FINAL_FIT.glob("*.bin"):
        if p.name == "y.bin":
            continue
        link = PREPARED / p.name
        if link.is_symlink() or link.exists():
            if link.resolve() != p.resolve():
                raise SystemExit(f"unexpected file at {link}")
        else:
            link.symlink_to(p.resolve())
    ynew = np.memmap(PREPARED / "y.bin.partial", dtype="<f4", mode="w+", shape=(n,))
    stnew = np.memmap(PREPARED / "label_status.u8.partial", dtype="u1", mode="w+", shape=(n,))
    origin = np.memmap(OUT / "prepared/label_origin.u8.partial", dtype="u1", mode="w+", shape=(n,))  # 1 search, 2 this run
    stats = Counter()
    overlap = {}
    per_day = {}
    for value in np.unique(fday):
        a, b = day_range(fday, value)
        ids = np.array(fid[a:b])
        if len(np.unique(ids)) != len(ids):
            raise SystemExit("duplicate stop IDs within a final date")
        sa, sb = day_range(sday_all, value)
        sids = sid.take(sa, sb)
        if len(sids) == len(ids) and np.array_equal(sids, ids):
            ynew[a:b] = sy.take(sa, sb)
            stnew[a:b] = sst.take(sa, sb)
            origin[a:b] = 1
            stats["rowwise_identical_rows"] += b - a
            continue
        order = np.argsort(sids, kind="stable")
        ss = sids[order]
        pos = np.searchsorted(ss, ids)
        pos_c = np.minimum(pos, max(len(ss) - 1, 0))
        matched = (pos < len(ss)) & (ss[pos_c] == ids) if len(ss) else np.zeros(len(ids), bool)
        local = order[pos_c[matched]]
        sy_day, sst_day = sy.take(sa, sb), sst.take(sa, sb)
        yy = np.empty(b - a, dtype="<f4")
        st = np.empty(b - a, dtype="u1")
        org = np.empty(b - a, dtype="u1")
        yy[matched], st[matched], org[matched] = sy_day[local], sst_day[local], 1
        unmatched = ~matched
        if unmatched.any():
            if int(value) not in corr_dates:
                raise SystemExit(f"{day_str(value)}: {int(unmatched.sum())} final rows have no corrected label")
            rows = np.arange(a, b)[unmatched] - base
            yy[unmatched], st[unmatched], org[unmatched] = cy[rows], cst[rows], 2
        if int(value) in corr_dates:
            # Where both corrections exist they must agree exactly.
            rows = np.arange(a, b)[matched] - base
            mismatch_y = int(np.count_nonzero(np.asarray(cy[rows]) != yy[matched]))
            mismatch_s = int(np.count_nonzero(np.asarray(cst[rows]) != st[matched]))
            overlap[day_str(value)] = {"rows": int(matched.sum()), "y_mismatch": mismatch_y, "status_mismatch": mismatch_s}
            if mismatch_y or mismatch_s:
                raise SystemExit(f"{day_str(value)}: this run's correction disagrees with the search correction "
                                 f"on overlapping rows (y {mismatch_y}, status {mismatch_s})")
        ynew[a:b], stnew[a:b], origin[a:b] = yy, st, org
        stats["joined_rows_from_search"] += int(matched.sum())
        stats["rows_from_this_run"] += int(unmatched.sum())
        stats["search_rows_not_in_final"] += int(len(ss) - matched.sum())
        per_day[day_str(value)] = {"rows": b - a, "from_search": int(matched.sum()), "from_this_run": int(unmatched.sum()),
                                   "search_rows_not_in_final": int(len(ss) - matched.sum())}
        log(f"{day_str(value)}: {per_day[day_str(value)]}")
    for value in np.setdiff1d(np.unique(sday_all), np.unique(fday)):
        sa, sb = day_range(sday_all, value)
        stats["search_rows_not_in_final"] += sb - sa
        per_day[day_str(value)] = {"rows": 0, "search_rows_not_in_final": sb - sa}
    for arr in (ynew, stnew, origin):
        arr.flush()
    retained_diff = changed = 0
    for s in range(0, n, 1 << 22):
        e = min(n, s + (1 << 22))
        y0, y1, st = np.asarray(fy[s:e]), np.asarray(ynew[s:e]), np.asarray(stnew[s:e])
        if not np.isfinite(y1).all():
            raise SystemExit("nonfinite corrected label")
        retained_diff += int(np.count_nonzero((y0 != y1) & (st != 2)))
        changed += int(np.count_nonzero(y0 != y1))
    if retained_diff:
        raise SystemExit(f"{retained_diff} rows not corrected (status != 2) differ from the original target")
    if stats["rowwise_identical_rows"] + stats["joined_rows_from_search"] + stats["rows_from_this_run"] != n:
        raise SystemExit("final rows not covered exactly once")
    del ynew, stnew, origin
    (PREPARED / "y.bin.partial").replace(PREPARED / "y.bin")
    (PREPARED / "label_status.u8.partial").replace(PREPARED / "label_status.u8")
    (OUT / "prepared/label_origin.u8.partial").replace(OUT / "prepared/label_origin.u8")
    st_all = np.fromfile(PREPARED / "label_status.u8", dtype="u1")
    v, cnt = np.unique(st_all, return_counts=True)
    status_counts = {str(int(k)): int(x) for k, x in zip(v, cnt)}
    del st_all
    columns = {name: {"bytes": (PREPARED / name).stat().st_size, "sha256": producer.sha(PREPARED / name)}
               for name in ("y.bin", "label_status.u8")}
    record = {"status": "complete", "policy": producer.POLICY,
              "population": "the final fit set's rows exactly; inputs are A's final fit files (symlinked)",
              "sources": {"search_correction": {"root": str(SEARCH.relative_to(ROOT)),
                                                "setup_sha": search_keys[0]["label_correction"]["setup_sha"],
                                                "progress_sha": search_keys[0]["label_correction"]["progress_sha"]},
                          "this_run": {"root": str(CORR.relative_to(ROOT)), "dates": corr["dates"],
                                       "columns": corr["columns"], "progress_sha": corr["progress_sha"]}},
              "join": "service date + stop ID", "rows": dict(stats), "overlap_check": overlap,
              "status_counts": status_counts, "labels_changed_vs_original": changed,
              "label_origin_sha256": producer.sha(OUT / "prepared/label_origin.u8"),
              "actual_dates": {"first": day_str(fday[0]), "last": day_str(fday[-1])}, "columns": columns}
    write_json(PREPARED / "meta.json", {**fit.meta, "label_correction": record})
    write_json(PREPARED / "key.json", {**fit.key, "label_correction": record})
    write_json(OUT / "prepared/labels-result.json", {**record, "per_day": per_day,
                                                     "peak_memory_bytes": int((cg / "memory.peak").read_text())})
    log(f"labels complete: {json.dumps(dict(stats))}; changed {changed:,}; status {status_counts}; overlap {overlap}")


# ---------------------------------------------------------------------------
# train
# ---------------------------------------------------------------------------


def a_inputs():
    a = manifest(A_DIR)
    return {"prep": a["prep"], "vocab": a["vocab"]}, a


def check_prepared():
    data = PreparedSet(PREPARED)
    final = PreparedSet(FINAL_FIT)
    if data.key["label_correction"]["status"] != "complete" or data.n != final.n:
        raise SystemExit("corrected prepared set incomplete")
    if {k: v for k, v in data.key.items() if k != "label_correction"} != final.key:
        raise SystemExit("corrected prepared key differs from the final fit key")
    for p in PREPARED.glob("*.bin"):
        if p.name != "y.bin" and p.resolve() != (FINAL_FIT / p.name).resolve():
            raise SystemExit(f"{p.name} is not A's final fit input")
    return data


def train(args):
    import torch
    import train_recency_delay as T
    from recency_weights import policy as recency_policy
    from dbrel.ml.arrival_nn import ArrivalNet, estimate_asset_bytes

    cg = guard()
    data = check_prepared()
    inputs, a = a_inputs()
    if _sha(inputs["prep"]) != data.key["prep"] or _sha(inputs["vocab"]) != data.key["vocab"]:
        raise SystemExit("A's manifest prep/vocab differ from the final fit encoding")
    if a["notice_rates"]["fingerprint"] != data.key["notice_rates"]["fingerprint"]:
        raise SystemExit("A's notice-rate record differs from the final fit encoding")
    cfg = T.configuration("distribution", T.TRIAL)[0]
    strong_key = json.loads((STRONG / "distribution/key.json").read_text())
    if cfg.as_dict() != strong_key["config"]:
        raise SystemExit("distribution configuration differs from the strong run")
    if strong_key["recency"]["amplitude"] != AMPLITUDE or strong_key["sampler"]["mix_blocks"] != T.MIX_BLOCKS \
            or strong_key["sampler"]["read_block_rows"] != T.BLOCK_ROWS:
        raise SystemExit("strong recipe constants differ")
    record = recency_policy(POLICY_END, AMPLITUDE)
    RUN.mkdir(parents=True, exist_ok=True)
    norm = T.WeightedSource(data, record, 0, len(data))
    try:
        mean_weight = norm.mean_weight
    finally:
        norm.close()
    key = {"version": 1,
           "imported": {name: sha_file(ROOT / name) for name in (
               "scripts/train_recency_delay.py", "scripts/recency_weights.py", "scripts/check_recency_discrete_delay.py",
               "scripts/check_discrete_delay.py", "src/dbrel/ml/arrival_nn.py", "src/dbrel/ml/arrival.py",
               "src/dbrel/ml/features.py", "src/dbrel/ml/full_year.py")},
           "recency": record, "config": cfg.as_dict(),
           "sampler": {"kind": "permuted-date-block-pools", "mix_blocks": T.MIX_BLOCKS, "read_block_rows": T.BLOCK_ROWS,
                       "pool_rows": T.BLOCK_ROWS * T.MIX_BLOCKS, "row_coverage": "exactly once per epoch",
                       "dataset_mean_weight": mean_weight},
           "selection": f"none: epoch {EPOCH} of {cfg.epochs}; monitor = first {MONITOR_ROWS:,} training rows (in-sample)",
           "prepared": str(PREPARED.relative_to(ROOT)), "prepared_key": data.key, "inputs": "A's manifest prep/vocab",
           "device": args.device, "torch_version": str(torch.__version__), "hip_version": torch.version.hip}
    if (RUN / "key.json").exists() and json.loads((RUN / "key.json").read_text()) != key:
        raise SystemExit("run setup changed; use a fresh directory")
    write_json(RUN / "key.json", key)
    if (RUN / "result.json").exists():
        log("already trained")
        return
    (RUN / "inputs.json").write_text(json.dumps(inputs, indent=1))
    if args.device != "cpu":
        if not torch.cuda.is_available():
            raise SystemExit("requested GPU is unavailable")
        torch.cuda.set_per_process_memory_fraction(0.60)
        torch.cuda.reset_peak_memory_stats()
    torch.set_num_threads(4)
    net = ArrivalNet(cfg, inputs["prep"])
    estimate = estimate_asset_bytes(cfg, inputs["prep"], inputs["vocab"])
    if estimate > 35_000_000:
        raise SystemExit(f"estimated asset {estimate:,} exceeds 35 MB")
    started = time.monotonic()
    source = T.WeightedSource(data, record, 0, len(data))
    try:
        if source.mean_weight != mean_weight:
            raise SystemExit("weights changed after setup")
        log(f"training {len(data):,} rows, recency {record}, mean weight {mean_weight:.6f}, estimate {estimate/1e6:.2f} MB")
        history, best = T.training(net, source, data, MONITOR_ROWS, args.device, RUN, key, None)
    finally:
        source.close()
    chosen = history[EPOCH - 1]
    if chosen["epoch"] != EPOCH or sha_file(RUN / f"epoch-{EPOCH:03d}.pt") != chosen["checkpoint_sha256"]:
        raise SystemExit("final-epoch checkpoint identity differs")
    result = {"training_rows": len(data), "epochs": cfg.epochs, "used_epoch": EPOCH,
              "in_sample_best_epoch_ignored": best, "recency": record, "dataset_mean_weight": mean_weight,
              "estimated_asset_bytes": estimate, "history_sha256": sha_file(RUN / "history.json"),
              "checkpoint": f"epoch-{EPOCH:03d}.pt", "checkpoint_sha256": chosen["checkpoint_sha256"],
              "final_epoch_monitor": chosen["selection_views"], "train_loss_by_epoch": [e["train_loss"] for e in history],
              "epoch_seconds": [e["seconds"] for e in history],
              "resources": T.resources(cg, time.monotonic() - started)}
    write_json(RUN / "result.json", result)
    log(f"finished: {json.dumps(result['resources'])}")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def sample_rows(data):
    index = np.linspace(0, len(data) - 1, 512, dtype=int)
    cats = np.concatenate([data.cats[int(i):int(i) + 1] for i in index])
    nums = np.concatenate([data.nums[int(i):int(i) + 1] for i in index])
    return np.concatenate([cats, np.zeros_like(cats[:64])]), np.concatenate([nums, nums[:64]])


def export(args):
    import onnx
    import onnxruntime as ort
    import torch
    from dbrel.ml.arrival import prep_numeric, save_arrival_artifact
    from dbrel.ml.arrival_nn import ArrivalNet, NetConfig, export_onnx
    from dbrel.ml.features import CATEGORICAL
    from dbrel.ml.full_year_search import write_browser_check
    from dbrel.ml.notice_rates import NoticeRates
    import check_recency_discrete_delay as D
    from check_discrete_delay import discrete_quantiles

    cg = guard()
    started = time.monotonic()
    data = check_prepared()
    key = json.loads((RUN / "key.json").read_text())
    trained = json.loads((RUN / "result.json").read_text())
    if (DISCRETE / "result.json").exists():
        log("export already complete; no writes")
        return
    ckpt = RUN / trained["checkpoint"]
    if sha_file(ckpt) != trained["checkpoint_sha256"]:
        raise SystemExit("checkpoint changed")
    inputs = json.loads((RUN / "inputs.json").read_text())
    cfg = NetConfig(**key["config"])
    net = ArrivalNet(cfg, inputs["prep"])
    net.model.load_state_dict(torch.load(ckpt, map_location="cpu", weights_only=True))
    net.model.eval()
    torch.set_num_threads(4)
    cats, nums = sample_rows(data)

    # Distribution (probabilities) export: ONNX vs torch tolerance 1e-4.
    if (RUN / "onnx").exists():
        (RUN / "onnx").rename(RUN / f"onnx-previous-{time.time_ns()}")
    dist = export_onnx(net, cats, nums, RUN / "onnx")
    dist_pass = dist["onnx_vs_torch_max_abs"] <= PROB_TOL
    write_browser_check(RUN / "browser_check", "distribution", dist, RUN / "onnx", cats, nums)

    # Discrete (quantiles) export, exactly as check_recency_discrete_delay.py.
    DISCRETE.mkdir(parents=True, exist_ok=True)
    if (DISCRETE / "onnx").exists():
        (DISCRETE / "onnx").rename(DISCRETE / f"onnx-previous-{time.time_ns()}")
    check = D.export(net, cats, nums, DISCRETE / "onnx")
    if not check["onnx_passed"]:
        raise SystemExit(f"discrete ONNX parity failed: {check['onnx_vs_torch_max_abs']}")
    write_browser_check(DISCRETE / "browser_check", "discrete", check, DISCRETE / "onnx", cats, nums)

    # Wider parity on 64 x 4,096 spread training rows.
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    opts.inter_op_num_threads = 1
    s_dist = ort.InferenceSession(str(RUN / "onnx/model.onnx"), opts, providers=["CPUExecutionProvider"])
    s_disc = ort.InferenceSession(str(DISCRETE / "onnx/model.onnx"), opts, providers=["CPUExecutionProvider"])
    module = D.DiscreteExport(net).cpu().eval()
    wide = {"rows": 0, "prob_max_abs": 0.0, "quantile_mismatches": 0, "median_mismatches": 0,
            "median_mismatches_numpy_rule_on_onnx_probs": 0}
    for s in np.linspace(0, len(data) - 4096, 64, dtype=int):
        c = np.ascontiguousarray(data.cats[int(s):int(s) + 4096], dtype=np.int64)
        x = np.ascontiguousarray(data.nums[int(s):int(s) + 4096], dtype=np.float32)
        with torch.inference_mode():
            probs_t = net._raw(torch.as_tensor(c), torch.as_tensor(x)).numpy()
            q_t = module(torch.as_tensor(c), torch.as_tensor(x)).numpy()
        probs_o = s_dist.run(["probs"], {"cat": c, "num": x})[0]
        q_o = s_disc.run(["quantiles"], {"cat": c, "num": x})[0]
        wide["rows"] += len(c)
        wide["prob_max_abs"] = max(wide["prob_max_abs"], float(np.abs(probs_o - probs_t).max()))
        wide["quantile_mismatches"] += int(np.count_nonzero(q_o != q_t))
        wide["median_mismatches"] += int(np.count_nonzero(q_o[:, 1] != q_t[:, 1]))
        wide["quantile_max_abs_min"] = max(wide.get("quantile_max_abs_min", 0.0), float(np.abs(q_o - q_t).max()))
        wide["median_mismatches_numpy_rule_on_onnx_probs"] += int(np.count_nonzero(
            discrete_quantiles(probs_o, net.edges)[:, 1] != q_o[:, 1]))
    # Gate as check_recency_discrete_delay.py does: exact discrete quantiles on the fixture rows
    # (D.export above). On the wide diagnostic rows the probabilities are gated at 1e-4; discrete
    # mismatches there are torch-vs-ONNX float differences at CDF ties and are reported, not gated
    # (the shipped ONNX output always equals the NumPy rule applied to its own probabilities).
    wide_pass = wide["prob_max_abs"] <= PROB_TOL and wide["median_mismatches_numpy_rule_on_onnx_probs"] == 0
    parity = {"distribution_sample": {k: v for k, v in dist.items() if k != "_expected"},
              "distribution_sample_passed_1e-4": dist_pass,
              "discrete_sample": {k: v for k, v in check.items() if k != "_expected"}, "wide": wide,
              "wide_passed": wide_pass}
    write_json(DISCRETE / "parity.json", parity)
    if not (dist_pass and wide_pass):
        raise SystemExit(f"parity failed: {json.dumps(parity)[:2000]}")

    # A's notice-rate record (same 321 dates), re-verified against the read-only database.
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite+pysqlite:///file:{DB}?mode=ro&uri=true", future=True)
    try:
        a_record = manifest(A_DIR)["notice_rates"]
        notice = NoticeRates.from_record(engine, a_record).record()
    finally:
        engine.dispose()
    if notice != a_record or notice["fingerprint"] != data.key["notice_rates"]["fingerprint"]:
        raise SystemExit("notice-rate record differs from A's")
    lc = data.key["label_correction"]
    man = {"recency": trained["recency"], "label_policy": lc["policy"],
           "source": {"experiment": str(DISCRETE.relative_to(ROOT)), "weights": str(RUN.relative_to(ROOT)),
                      "prepared": str(PREPARED.relative_to(ROOT)), "inputs_from": str(A_DIR.relative_to(ROOT)),
                      "recipe": "recency_training_v3_20261014 strong distribution + discrete"},
           "config": cfg.as_dict(), "prep": inputs["prep"], "vocab": inputs["vocab"],
           "output": "quantiles", "edges": None, "point_rule": "median",
           "postprocess": {"kind": "discrete conditional quantiles", "target_resolution_min": 1.0,
                           "cdf_boundary_tolerance": D.CDF_TOLERANCE,
                           "end_bins": "training labels outside [-30,120) use endpoint classes"},
           "features": list(CATEGORICAL) + list(prep_numeric(inputs["prep"])), "notice_rates": notice,
           "training": {"rows": trained["training_rows"], "dates": [lc["actual_dates"]["first"], lc["actual_dates"]["last"]],
                        "epochs": trained["epochs"], "used_epoch": trained["used_epoch"], "selection": "none"},
           "scores": {"in_sample_monitor_final_epoch": trained["final_epoch_monitor"],
                      "note": "no held-out validation: refit on all rows through 2026-09-19; test-week scores in docs/live-vs-refit-results.md"},
           "onnx_check": parity["discrete_sample"],
           "disclosure": ["refit on all data through 2026-09-19 with no held-out selection; fixed 12 epochs",
                          "minute-valued targets; integer CDF quantiles replace within-bin interpolation",
                          "CDF ties use a fixed 1e-6 probability tolerance", "historical five-hour-cutoff replay",
                          "present-day timetable patterns; historical timetable-as-of not verified"]}
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifact = save_arrival_artifact(ARTIFACTS, man, {"model.onnx": DISCRETE / "onnx/model.onnx"}, stamp,
                                     role="deployable", served_dir=ROOT / "models/v2")
    measured = json.loads((artifact / "manifest.json").read_text())["download_bytes"]
    result = {"artifact": {"path": str(artifact.relative_to(ROOT)), "download_bytes": measured, "role": "deployable",
                           "fits_strict_35mb": measured <= 35_000_000},
              "parity": parity, "onnx_version": onnx.__version__, "onnxruntime_version": ort.__version__,
              "export_identity": {str(p.relative_to(ROOT)): sha_file(p) for p in
                                  (DISCRETE / "onnx/model.onnx", DISCRETE / "onnx/prep.json", artifact / "model.onnx",
                                   artifact / "manifest.json", RUN / "onnx/model.onnx")},
              "resources": {"seconds": time.monotonic() - started, "cgroup_peak_bytes": int((cg / "memory.peak").read_text())}}
    write_json(DISCRETE / "result.json", result)
    log(json.dumps(result["artifact"]))


# ---------------------------------------------------------------------------
# predict
# ---------------------------------------------------------------------------


def predict(args):
    from compare_live_vs_strong import onnx_session, run_onnx, basic_score, compare, TOL
    from dbrel.ml.features import CATEGORICAL
    cg = guard()
    PRED.mkdir(parents=True, exist_ok=True)
    exported = json.loads((DISCRETE / "result.json").read_text())
    b_dir = ROOT / exported["artifact"]["path"]
    for name, expected in exported["export_identity"].items():
        if sha_file(ROOT / name) != expected:
            raise SystemExit(f"export changed: {name}")
    test = PreparedSet(TEST)
    if test.n != 2_210_206:
        raise SystemExit(f"unexpected test rows {test.n}")
    ma, mb = manifest(A_DIR), manifest(b_dir)
    for k, m in (("A", ma), ("B", mb)):
        if m["output"] != "quantiles" or m["point_rule"] != "median" or m["kind"] != "arrival-net":
            raise SystemExit(f"{k}: unexpected output contract")
    # New B uses A's encoding: the test cache is exactly both models' input.
    if _sha(mb["vocab"]) != test.key["vocab"] or _sha(mb["prep"]) != test.key["prep"] \
            or mb["notice_rates"] != ma["notice_rates"] or ma["features"] != mb["features"] \
            or _sha(ma["vocab"]) != test.key["vocab"] or _sha(ma["prep"]) != test.key["prep"] \
            or test.key["notice_rates"]["fingerprint"] != ma["notice_rates"]["fingerprint"]:
        raise SystemExit("test cache is not A's (= new B's) encoding")
    cats, nums = np.asarray(test.cats), np.asarray(test.nums)
    y = np.asarray(test.y, dtype=np.float64)
    out = {}
    for k, d in (("A", A_DIR), ("B", b_dir)):
        q = run_onnx(onnx_session(d, args.threads), cats, nums)
        np.save(PRED / f"{k}_quantiles.npy", q)
        out[k] = q
        log(f"{k} predicted")
    # A sanity, re-verified: final.json score and the earlier head-to-head's A predictions.
    final = json.loads(FINAL_JSON.read_text())
    a_check = compare(final["scores"]["compact-winner"]["overall"], basic_score(y, out["A"][:, 1]))
    lvs = json.loads((LVS / "predictions/sanity.json").read_text())
    for k in ("A", "B"):
        if sha_file(LVS / f"predictions/{k}_quantiles.npy") != lvs["predictions_sha256"][k]:
            raise SystemExit(f"earlier {k} predictions changed")
    prior_a = np.load(LVS / "predictions/A_quantiles.npy")
    a_vs_prior = float(np.abs(prior_a - out["A"]).max())
    shutil.copyfile(LVS / "predictions/B_quantiles.npy", PRED / "B0_quantiles.npy")
    a_pass = all(v["abs_diff"] <= TOL for v in a_check.values()) and a_vs_prior <= TOL
    sanity = {"A_vs_final_json": a_check, "A_passed": a_pass, "A_max_abs_vs_earlier_run": a_vs_prior,
              "earlier_head_to_head_sanity": {"A_passed": lvs["A_passed"], "B_passed": lvs["B_passed"],
                                              "predictions_sha256": lvs["predictions_sha256"]},
              "B_encoding": "A's test cache (vocab/prep/notice record identical by manifest)",
              "unknown_category_share": {c: float((cats[:, i] == 0).mean()) for i, c in enumerate(CATEGORICAL)},
              "artifacts": {k: {"dir": str(d.relative_to(ROOT)), "model_sha256": sha_file(d / "model.onnx"),
                                "manifest_sha256": sha_file(d / "manifest.json")} for k, d in (("A", A_DIR), ("B", b_dir), ("B0", B0_DIR))},
              "predictions_sha256": {k: sha_file(PRED / f"{k}_quantiles.npy") for k in ("A", "B", "B0")},
              "peak_memory_bytes": int((cg / "memory.peak").read_text())}
    write_json(PRED / "sanity.json", sanity)
    log(f"A sanity: {json.dumps(a_check)}; vs earlier run {a_vs_prior}")
    if not a_pass:
        raise SystemExit("A does not reproduce final.json / the earlier run")


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------

MODELS = ("A", "B", "B0")
LABEL = {"A": "A live (20261004T213106Z)", "B": "B refit (new)", "B0": "earlier B (20261014T091943Z)"}


def score(args):
    from summarize_recency_training import Score, paired_day_bootstrap, shown, DAY_BOOTSTRAP_SEED, DAY_BOOTSTRAP_REPLICATES
    if DAY_BOOTSTRAP_SEED != 4231 or DAY_BOOTSTRAP_REPLICATES != 2000:
        raise SystemExit("summarizer bootstrap constants changed")
    test = PreparedSet(TEST)
    sanity = json.loads((PRED / "sanity.json").read_text())
    if not sanity["A_passed"]:
        raise SystemExit("A sanity did not pass")
    for k in MODELS:
        if sha_file(PRED / f"{k}_quantiles.npy") != sanity["predictions_sha256"][k]:
            raise SystemExit("prediction file changed")
    pct = {k: np.load(PRED / f"{k}_quantiles.npy") for k in MODELS}
    points = {k: pct[k][:, 1].astype(np.float64) for k in MODELS}
    day = np.asarray(test.column("day"))
    res = json.loads((LVS / "corrected/result.json").read_text())
    if sha_file(LVS / "corrected/y.bin") != res["columns"]["y.bin"]:
        raise SystemExit("corrected test labels changed")
    labels_ = {"corrected": np.fromfile(LVS / "corrected/y.bin", dtype="<f4").astype(np.float64),
               "original": np.asarray(test.y, dtype=np.float64)}
    corr_status = np.fromfile(LVS / "corrected/label_status.u8", dtype="u1")
    if len(labels_["corrected"]) != test.n or len(corr_status) != test.n:
        raise SystemExit("corrected label count differs")
    browser = {}
    for name, d in (("distribution", RUN), ("discrete", DISCRETE)):
        p = d / "actual_frontend_browser.json"
        if p.exists():
            rep = json.loads(p.read_text())
            browser[name] = {"passed": rep.get("passed"), "runtime_version": rep.get("runtime_version"),
                             "chromium": rep.get("chromium"),
                             "cases": {c: {k: v.get(k) for k in ("rows", "max_abs_vs_python", "single_vs_batch_max_abs")}
                                       for c, v in rep.get("cases", {}).items()},
                             "probability_metrics": rep.get("probability_metrics")}
    results = {"rows": int(test.n), "dates": [day_str(d) for d in np.unique(day)], "label_sets": {}, "sanity": sanity,
               "training": json.loads((RUN / "result.json").read_text()),
               "export": json.loads((DISCRETE / "result.json").read_text()), "browser": browser}
    for label_name, y in labels_.items():
        subsets = {"label_changed": corr_status == 2, "label_unchanged": corr_status != 2}
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
                    day_sums[k][view][day_str(dv)] = ds.sufficient()
                    entry["by_day"].setdefault(day_str(dv), {}).setdefault(k, {})[view] = ds.result()
                for sub, mask in subsets.items():
                    ss = Score()
                    ss.add(y[mask], pred[mask])
                    entry["subsets"].setdefault(sub, {}).setdefault(k, {})[view] = ss.result()
            band = (y >= pct[k][:, 0]) & (y <= pct[k][:, 2])
            entry["overall"][k]["band_coverage_p25_p75"] = float(band.mean())
        entry["bootstrap_B_vs_A"] = paired_day_bootstrap(day_sums["A"], day_sums["B"], variant="B")
        entry["bootstrap_B_vs_B0"] = paired_day_bootstrap(day_sums["B0"], day_sums["B"], variant="B")
        entry["bootstrap_B0_vs_A"] = paired_day_bootstrap(day_sums["A"], day_sums["B0"], variant="B0")
        results["label_sets"][label_name] = entry
    v, c = np.unique(corr_status, return_counts=True)
    results["correction_status_counts"] = {str(int(s)): int(x) for s, x in zip(v, c)}
    results["corrected_label_rows_changed"] = int((labels_["corrected"] != labels_["original"]).sum())
    results["labels_result"] = json.loads((OUT / "prepared/labels-result.json").read_text())
    results["correction_progress"] = {d: v["counts"] for d, v in json.loads((CORR / "progress.json").read_text()).items()}
    write_json(OUT / "results.json", results)
    DOC.write_text(build_doc(results))
    log(f"wrote {OUT / 'results.json'} and {DOC}")


def build_doc(r):
    def pct(v):
        return f"{100 * v:.2f}%"

    def pp(v):
        return f"{100 * v:+.2f} pp"

    def ci_pp(ci):
        return f"[{pp(ci[0])}, {pp(ci[1])}]"

    def excl(ci):
        return ci[0] > 0 or ci[1] < 0

    sets = r["label_sets"]
    c = sets["corrected"]["bootstrap_B_vs_A"]["B_minus_control"]["displayed"]
    o = sets["original"]["bootstrap_B_vs_A"]["B_minus_control"]["displayed"]
    w5, w1, mae = c["within_5_min"], c["within_1_min"], c["mae_scored"]
    better = w5["B_minus_control"] > 0
    tr, ex, lr = r["training"], r["export"], r["labels_result"]
    art = ex["artifact"]["path"]
    lines = ["# Live vs refit: strong discrete model retrained through 2026-09-19", "", "## Summary", ""]
    lines.append(
        f"- **On corrected labels, as the app displays them, {'the refit (B) is better than' if better else 'the live model (A) is better than'} "
        f"{'the live model (A)' if better else 'the refit (B)'}.** Right to within 5 minutes: A {pct(w5['control'])} vs "
        f"B {pct(w5['B'])} ({pp(w5['B_minus_control'])}; 95% service-date interval {ci_pp(w5['ci95'])}"
        f"{'' if excl(w5['ci95']) else ', which includes zero'}). Within 1 minute: A {pct(w1['control'])} vs B {pct(w1['B'])} "
        f"({pp(w1['B_minus_control'])}, {ci_pp(w1['ci95'])}). Average error: A {mae['control']:.2f} vs B {mae['B']:.2f} min "
        f"({mae['B_minus_control']:+.3f} min, [{mae['ci95'][0]:+.3f}, {mae['ci95'][1]:+.3f}]).")
    lines.append(
        f"- **On the original (uncorrected) labels**, the label definition A was trained on: within 5 minutes A "
        f"{pct(o['within_5_min']['control'])} vs B {pct(o['within_5_min']['B'])} ({pp(o['within_5_min']['B_minus_control'])}, "
        f"{ci_pp(o['within_5_min']['ci95'])}); MAE A {o['mae_scored']['control']:.2f} vs B {o['mae_scored']['B']:.2f}.")
    e0 = sets["corrected"]["bootstrap_B_vs_B0"]["B_minus_control"]["displayed"]
    lines.append(
        f"- **What the extra four weeks added** (refit B vs the earlier strong B trained through 2026-08-21; corrected labels, "
        f"displayed): within 5 min {pct(e0['within_5_min']['control'])} → {pct(e0['within_5_min']['B'])} "
        f"({pp(e0['within_5_min']['B_minus_control'])}, {ci_pp(e0['within_5_min']['ci95'])}); MAE "
        f"{e0['mae_scored']['control']:.2f} → {e0['mae_scored']['B']:.2f}. The refit also uses A's input encoding "
        "(vocabulary, scaling, notice rates over 321 dates) instead of the earlier search encoding.")
    lines += ["",
              "All three models predicted the same 2,210,206 arrivals of 2026-09-20..26, a week none of them trained on.",
              "",
              "- **A (live):** `models/v2/20261004T213106Z`, quantile MLP, trained 2025-11-03..2026-09-19 on original labels.",
              f"- **B (refit, new):** `{art}`. The strong discrete recipe (binned classifier, seed 1, 12 epochs, no epoch "
              f"selection, recency weights rising linearly to 3x over {tr['recency']['start']}..{tr['recency']['end']}) trained on "
              f"A's exact {tr['training_rows']:,} rows and inputs, with version-2 dated-plan corrected labels on every row.",
              "- **Earlier B:** `data/ml/recency_training_v3_20261014/strong/runs/discrete/artifacts/20261014T091943Z`, the same "
              "recipe trained on corrected labels 2025-11-03..2026-08-21 with the search encoding; reference column only.",
              "",
              "'Displayed' applies the app's rule (`shown` in `scripts/summarize_recency_training.py`): below 1 minute shows "
              "as 0, otherwise round half up to a whole minute. The headline comparison is B vs A, displayed.", ""]
    for name in ("corrected", "original"):
        e = sets[name]
        lines += [f"## Scores on {name} labels", "",
                  "| Model | View | Within 1 min | Within 3 min | Within 5 min | MAE (min) | Inside p25–p75 |",
                  "|---|---|---:|---:|---:|---:|---:|"]
        for k in MODELS:
            for view in ("raw", "displayed"):
                s = e["overall"][k][view]
                lines.append(f"| {LABEL[k]} | {view} | {pct(s['within_1_min'])} | {pct(s['within_3_min'])} | "
                             f"{pct(s['within_5_min'])} | {s['mae_scored']:.4f} | {pct(e['overall'][k]['band_coverage_p25_p75'])} |")
        for title, key, ctrl in ((f"B (refit) − A (live), paired service-date bootstrap ({name} labels)", "bootstrap_B_vs_A", "A"),
                                 (f"B (refit) − earlier B ({name} labels)", "bootstrap_B_vs_B0", "earlier B")):
            lines += ["", f"### {title}", "",
                      "2,000 paired resamples of the 7 service dates (seed 4231), percentile 95% interval "
                      "(`summarize_recency_training.paired_day_bootstrap`). Positive within-rate and negative MAE "
                      "differences favour the refit.", "",
                      f"| View | Metric | {ctrl} | B (refit) | Difference | 95% interval |", "|---|---|---:|---:|---:|---|"]
            for view in ("raw", "displayed"):
                for metric, est in e[key]["B_minus_control"][view].items():
                    if metric == "mae_scored":
                        lines.append(f"| {view} | MAE (min) | {est['control']:.4f} | {est['B']:.4f} | {est['B_minus_control']:+.4f} | "
                                     f"[{est['ci95'][0]:+.4f}, {est['ci95'][1]:+.4f}] |")
                    else:
                        lines.append(f"| {view} | {metric.replace('_min', ' min').replace('_', ' ')} | {pct(est['control'])} | "
                                     f"{pct(est['B'])} | {pp(est['B_minus_control'])} | {ci_pp(est['ci95'])} |")
        lines += ["", f"### By service date ({name} labels, displayed)", "",
                  "| Date | Rows | A within 5 | B within 5 | earlier B within 5 | A MAE | B MAE | earlier B MAE |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for d, v in e["by_day"].items():
            a, b, b0 = v["A"]["displayed"], v["B"]["displayed"], v["B0"]["displayed"]
            lines.append(f"| {d} | {a['eligible']:,} | {pct(a['within_5_min'])} | {pct(b['within_5_min'])} | {pct(b0['within_5_min'])} | "
                         f"{a['mae_scored']:.3f} | {b['mae_scored']:.3f} | {b0['mae_scored']:.3f} |")
        titles = {"label_changed": "Arrivals whose label the correction changed (status 2)",
                  "label_unchanged": "Arrivals whose label is the same in both label sets"}
        for sub, v in e["subsets"].items():
            lines += ["", f"### {titles[sub]}, {name} labels, displayed", "",
                      "| Model | Rows | Within 1 min | Within 5 min | MAE (min) |", "|---|---:|---:|---:|---:|"]
            for k in MODELS:
                s = v[k]["displayed"]
                lines.append(f"| {LABEL[k]} | {s['eligible']:,} | {pct(s['within_1_min'])} | {pct(s['within_5_min'])} | {s['mae_scored']:.3f} |")
        lines.append("")
    s = r["sanity"]
    rows = lr["rows"]
    prog = r["correction_progress"]
    overlap = "; ".join(f"{d}: {v['rows']:,} rows, {v['y_mismatch']} label and {v['status_mismatch']} status differences"
                        for d, v in lr["overlap_check"].items())
    br = r.get("browser", {})
    browser_line = "; ".join(
        f"{name}: {'passed' if v.get('passed') else 'FAILED'} (ONNX Runtime Web {v.get('runtime_version')}, "
        + ", ".join(f"{c} max |web−python| {cv['max_abs_vs_python']:.2e} min" for c, cv in v.get("cases", {}).items()
                    if cv.get("max_abs_vs_python") is not None) + ")"
        for name, v in br.items()) or "not recorded"
    res_ = tr["resources"]
    lines += ["## How the refit was made", "",
              f"- **Rows and inputs:** A's final fit set (`data/ml/full_year/prepared/final-2026-09-26-robust-no-doy-mc20-rd56-fit`, "
              f"{tr['training_rows']:,} rows, service dates {lr['actual_dates']['first']}..{lr['actual_dates']['last']}; "
              "the training split's finalization cutoff of 2026-09-19 17:07 UTC leaves no 2026-09-19 rows) with A's vocabulary, "
              "prep and notice-rate record (fingerprint `1e57bfee…`, 321 dates 2025-11-03..2026-09-19, re-verified against the "
              "read-only database). Feature files are A's, byte for byte (symlinked).",
              f"- **Labels:** version-2 dated-plan corrections joined on service date + stop ID. "
              f"{rows.get('rowwise_identical_rows', 0) + rows.get('joined_rows_from_search', 0):,} rows take the existing search "
              f"correction (`data/ml/trip_schedule_correction_20261013`); {rows.get('rows_from_this_run', 0):,} rows were corrected in "
              "this run (the part of 2026-08-21 and all of 2026-08-22 that the search split's earlier finalization cutoff left out) "
              "with the unchanged producer logic and the d-2..d+2 mirror-folder selection. Where both corrections cover a row they "
              f"agree exactly ({overlap}). Rows not corrected (status ≠ 2) carry the original target exactly. Labels changed vs "
              f"original: {lr['labels_changed_vs_original']:,} rows. Status counts: {lr['status_counts']}.",
              "- **This run's correction counts:** " + "; ".join(f"{d}: {json.dumps(v)}" for d, v in prog.items()) + ".",
              f"- **Training:** the pinned `train_recency_delay.training` with `WeightedSource` (permuted pools of 8 blocks × 262,144 "
              f"rows, fixed full-dataset mean weight {tr['dataset_mean_weight']:.4f}), recency policy "
              f"{tr['recency']['start']}..{tr['recency']['end']} amplitude 2.0, `configuration('distribution')`, 12 epochs, "
              f"epoch 12 used (no selection). {res_['seconds'] / 60:.1f} min on the ROCm GPU; cgroup memory peak "
              f"{res_['cgroup_peak_bytes'] / 2**30:.2f} GiB (cap 8 GiB), GPU peak {res_['gpu_peak_allocated_bytes'] / 2**20:.0f} MiB.",
              f"- **Export:** discrete p25/p50/p75 baked into ONNX by `check_recency_discrete_delay.DiscreteExport`, gated as that "
              f"script gates it: ONNX equals torch exactly on the 576 fixture rows (max difference "
              f"{ex['parity']['discrete_sample']['onnx_vs_torch_max_abs']}). Extra diagnostic on {ex['parity']['wide']['rows']:,} "
              f"spread training rows: distribution ONNX probabilities within {ex['parity']['wide']['prob_max_abs']:.1e} of torch "
              f"(tolerance 1e-4); {ex['parity']['wide']['quantile_mismatches']} of {3 * ex['parity']['wide']['rows']:,} discrete "
              f"quantile values ({ex['parity']['wide']['median_mismatches']} medians) differ from torch by "
              f"{ex['parity']['wide'].get('quantile_max_abs_min', 0):.0f} minute, where a cumulative probability sits within float "
              "rounding of a quartile; the ONNX output always equals the NumPy discrete rule applied to the ONNX probabilities "
              f"({ex['parity']['wide']['median_mismatches_numpy_rule_on_onnx_probs']} differences). Artifact `{art}`, "
              f"{ex['artifact']['download_bytes']:,} bytes, role deployable.",
              f"- **Browser check** (pinned Chromium, actual frontend predictor, 0.01-min tolerance): {browser_line}.",
              f"- **Check on A:** re-run on the test cache it reproduces final.json (within 5 difference "
              f"{s['A_vs_final_json']['within_5_min']['abs_diff']:.1e}, MAE {s['A_vs_final_json']['mae_scored']['abs_diff']:.1e}) "
              f"and the earlier head-to-head's A predictions (max difference {s['A_max_abs_vs_earlier_run']:.1e}).",
              "- B (refit) reads exactly the same encoded test inputs as A (identical vocabulary, prep and notice record). "
              "Earlier-B predictions come from `data/ml/live_vs_strong_20261014/predictions` (hash-checked).",
              f"- **Corrected test labels:** `data/ml/live_vs_strong_20261014/corrected/` (changed on "
              f"{r['corrected_label_rows_changed']:,} arrivals).",
              "", "## Caveats", "",
              "- Seven service dates only; the bootstrap clusters by date, so intervals are rough and say nothing about other weeks.",
              "- Nothing was selected on held-out data for the refit: the recipe and the 12 epochs come from the earlier strong run. "
              "The per-epoch monitor numbers in the run directory are in-sample.",
              "- Original labels favour A's training definition; corrected labels favour B's.",
              "- The recency policy ends on 2026-09-19 (the training split's last date) although the last date with finalized "
              "rows is 2026-09-18; the earlier strong run ended its policy on its last date with rows. The effect is a "
              "one-day shift of a linear ramp (about 1/31 of the 2x ramp).",
              "- The extra-month comparison (refit vs earlier B) changes two things at once: four more weeks of data and A's "
              "input encoding instead of the search encoding.",
              "- B outputs whole-minute quantiles; A's are continuous. The displayed view is the fair comparison for what users see.",
              "- Historical five-hour-cutoff replay with present-day timetable patterns, not a logged prospective forecast.",
              "- Nothing in production changed: CURRENT, the deployed database and other experiments are untouched.", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("correct")
    sub.add_parser("labels")
    p = sub.add_parser("train")
    p.add_argument("--device", default="cuda", choices=("cuda", "cpu"))
    sub.add_parser("export")
    p = sub.add_parser("predict")
    p.add_argument("--threads", type=int, default=8)
    sub.add_parser("score")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    {"correct": correct, "labels": labels, "train": train, "export": export, "predict": predict,
     "score": score}[args.command](args)


if __name__ == "__main__":
    main()
