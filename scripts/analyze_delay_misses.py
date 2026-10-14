"""Read-only, bounded diagnostics of saved delay predictions before retraining.

Only reference predictions carry independently checked ordered stop IDs.
Candidate location breakdowns are exploratory until producer identity binding
is supplied; the published report deliberately excludes those breakdowns.
"""
from __future__ import annotations

import json
import csv
import os
import resource
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from compact_provenance import verify_training_key
from explore_compact_delay import guard
from summarize_compact_training import shown

EXPERIMENT = ROOT / "data/ml/compact_training_20261013"
PREPARED = ROOT / "data/ml/full_year/prepared/search-2026-09-26-robust-no-doy-mc20-rd56-eval"
REFERENCE = ROOT / "data/ml/full_year/runs/compact-2026-09-26/trials/f1ac12b1742e3a0b/validation.npz"
OUT = ROOT / "data/ml/delay_miss_diagnostics_20261013"
BLOCK = 131072
TRACKS = {"fine": "median", "discrete": "median", "distribution": "window3"}


class Group:
    def __init__(self, size):
        self.n = np.zeros(size, dtype=np.int64)
        self.values = {}

    def add(self, code, errors):
        self.n += np.bincount(code, minlength=len(self.n))
        for name, signed in errors.items():
            absolute = np.abs(signed)
            if name not in self.values:
                self.values[name] = np.zeros((5, len(self.n)), dtype=np.float64)
            value = self.values[name]
            for row, weight in enumerate((absolute > 5, signed > 5, signed < -5, absolute, absolute > 15)):
                value[row] += np.bincount(code, weights=weight, minlength=len(self.n))

    def records(self, name, label):
        value = self.values[name]
        total_misses = float(value[0].sum())
        return [{"key": int(i), "label": label(int(i)), "arrivals": int(self.n[i]),
                 "misses": int(value[0, i]), "miss_rate": float(value[0, i]/self.n[i]),
                 "share_of_misses": float(value[0, i]/total_misses) if total_misses else 0,
                 "underestimated": int(value[1, i]), "overestimated": int(value[2, i]),
                 "mae": float(value[3, i]/self.n[i]), "errors_over_15": int(value[4, i])}
                for i in np.flatnonzero(self.n)]


def ranks(records, minimum=1000, limit=15):
    return {"by_count": sorted(records, key=lambda r: (-r["misses"], r["key"]))[:limit],
            "by_rate": sorted((r for r in records if r["arrivals"] >= minimum),
                              key=lambda r: (-r["miss_rate"], -r["arrivals"], r["key"]))[:limit],
            "minimum_arrivals_for_rate": minimum}


def lookup(connection, table, ids, columns):
    out = {}
    for a in range(0, len(ids), 400):
        chunk = ids[a:a+400]
        rows = connection.execute(f"SELECT id,{columns} FROM {table} WHERE id IN ({','.join('?' for _ in chunk)})", chunk)
        out.update({int(row[0]): tuple(row[1:]) for row in rows})
    return out


def main():
    cgroup = guard()
    if int((cgroup / "memory.max").read_text()) > 2*2**30:
        raise ValueError("diagnostics require a private <=2 GiB cgroup")
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "result.json").exists():
        raise ValueError("completed diagnostics are immutable; choose a fresh output directory")
    keys = {name: json.loads((EXPERIMENT / "runs" / name / "key.json").read_text()) for name in TRACKS}
    inputs = verify_training_key(keys["fine"], ROOT)
    for name, key in keys.items():
        frozen = key["source_key"] if name == "discrete" else key
        verify_training_key(frozen, ROOT)
        if frozen["validation"] != keys["fine"]["validation"]:
            raise ValueError("candidates refer to different validation rows")
    vocab = inputs["vocab"]
    meta = json.loads((PREPARED / "meta.json").read_text())
    n = meta["n"]
    days = np.memmap(PREPARED / "day.bin", dtype="<i4", mode="r", shape=(n,))
    boundary = int(np.searchsorted(days, int(np.datetime64("2026-09-06", "D").astype(int))))
    with np.load(REFERENCE) as z:
        ids, reference = z["stop_id"], z["point"]
    actual_ids = np.memmap(PREPARED / "stop_id.bin", dtype="<i8", mode="r", shape=(n,))
    if ids.shape != actual_ids.shape or reference.shape != (n,):
        raise ValueError("reference size differs")
    for a in range(0, n, BLOCK):
        if not np.array_equal(ids[a:a+BLOCK], actual_ids[a:a+BLOCK]):
            raise ValueError("reference stop IDs differ")
    reference = reference[boundary:]
    del ids
    predictions = {"reference": reference, **{name: np.load(EXPERIMENT / "runs" / name / f"{rule}.npy", mmap_mode="r")
                    for name, rule in TRACKS.items()}}
    if any(p.shape != reference.shape for p in predictions.values()):
        raise ValueError("prediction row counts differ")
    calendar = np.unique(days[boundary:])
    station_size = len(vocab["station"])+1
    groups = {"day": Group(len(calendar)), "station": Group(station_size),
              "state": Group(len(vocab["state"])+1), "segment": Group(3), "hour": Group(25),
              "train": Group(len(vocab["train"])+1), "line": Group(len(vocab["line"])+1),
              "weekday": Group(8), "novelty": Group(4), "actual_delay": Group(7),
              "station_day": Group(station_size*len(calendar)), "day_segment": Group(3*len(calendar)), "total": Group(1)}
    stations = None
    connection = sqlite3.connect(f"file:{ROOT / 'data/dbrel.sqlite'}?mode=ro", uri=True)
    try:
        stations = lookup(connection, "station", vocab["station"], "name,state,lat,lon")
        # Independently verify that vocabulary slots decode real station IDs.
        sample = np.linspace(boundary, n-1, 64, dtype=int)
        sample_ids = [int(actual_ids[i]) for i in sample]
        check = lookup(connection, "trip_stop", sample_ids, "station_id")
        for i in sample:
            slot = int(np.fromfile(PREPARED / "cats.bin", dtype="<i4", count=1, offset=int(i)*8*4)[0])
            if slot and check[int(actual_ids[i])][0] != vocab["station"][slot-1]:
                raise ValueError("station vocabulary decoding differs from database")
        for start in range(boundary, n, BLOCK):
            stop = min(n, start+BLOCK)
            length = stop-start
            cats = np.fromfile(PREPARED / "cats.bin", dtype="<i4", count=length*8, offset=start*8*4).reshape(length, 8)
            y = np.fromfile(PREPARED / "y.bin", dtype="<f4", count=length, offset=start*4).astype(np.float64)
            segment = np.fromfile(PREPARED / "segment.bin", dtype="<i2", count=length, offset=start*2)
            minute = np.fromfile(PREPARED / "sched_min.bin", dtype="<f4", count=length, offset=start*4)
            day = np.searchsorted(calendar, days[start:stop])
            code = {"day": day, "station": cats[:, 0], "train": cats[:, 1], "line": cats[:, 2],
                    "state": cats[:, 5], "weekday": cats[:, 6], "segment": segment,
                    "hour": np.where(np.isfinite(minute), np.clip(np.floor(minute/60), 0, 23), 24).astype(int),
                    "novelty": (cats[:, 1] == 0).astype(int)+2*(cats[:, 0] == 0),
                    "actual_delay": np.searchsorted([-5, 0, 1, 5, 15, 30], y, side="right"),
                    "station_day": day*station_size+cats[:, 0], "day_segment": day*3+segment,
                    "total": np.zeros(length, dtype=int)}
            errors = {}
            for name, point in predictions.items():
                p = np.asarray(point[start-boundary:stop-boundary], dtype=np.float64)
                if not np.isfinite(p).all() or not np.isfinite(y).all():
                    raise ValueError("every row needs a finite target and prediction")
                errors[f"{name}_raw"], errors[f"{name}_display"] = y-p, y-shown(p)
            for group, values in code.items():
                if np.any(values < 0) or np.any(values >= len(groups[group].n)):
                    raise ValueError(f"invalid {group} key")
                groups[group].add(values, errors)
            if (start-boundary)//BLOCK % 8 == 0:
                print(f"Grouped {stop-boundary:,}/{n-boundary:,} arrivals", flush=True)
        train_slots = np.flatnonzero(groups["train"].n)
        train_ids = [vocab["train"][int(i)-1] for i in train_slots if i]
        trains = lookup(connection, "train", train_ids, "display_name,category,train_number")
        lines = lookup(connection, "service_line", vocab["line"], "display_name")
    finally:
        connection.close()

    def station_name(slot):
        if not slot:
            return "UNKNOWN station (not in training vocabulary)"
        ident = vocab["station"][slot-1]
        return f"{stations.get(ident, (str(ident),))[0]} [id {ident}]"

    def train_name(slot):
        if not slot:
            return "UNKNOWN train"
        ident = vocab["train"][slot-1]
        display, category, number = trains.get(ident, (None, "", ident))
        return f"{display or f'{category} {number}'} [id {ident}]"

    labels = {"day": lambda i: str(np.datetime64(int(calendar[i]), "D")), "station": station_name,
              "state": lambda i: (vocab["state"][i-1] or "Unknown/cross-border") if i else "UNKNOWN state",
              "train": train_name, "line": lambda i: ("No line" if vocab["line"][i-1] == -1 else lines.get(vocab["line"][i-1], (str(vocab["line"][i-1]),))[0]) if i else "UNKNOWN line",
              "segment": lambda i: ["Regional", "Long-distance", "S-Bahn"][i],
              "hour": lambda i: f"{i:02}:00–{i:02}:59" if i < 24 else "Unknown hour",
              "weekday": lambda i: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][vocab["weekday"][i-1]] if i else "UNKNOWN weekday",
              "novelty": lambda i: ["Known train / known station", "Unknown train / known station", "Known train / unknown station", "Unknown train / unknown station"][i],
              "actual_delay": lambda i: ["Earlier than -5 min", "-5 to -1 min", "Exactly on time", "1–4 min late", "5–14 min late", "15–29 min late", "30+ min late"][i],
              "day_segment": lambda i: f"{str(np.datetime64(int(calendar[i//3]), 'D'))} / {['Regional', 'Long-distance', 'S-Bahn'][i%3]}",
              "total": lambda i: "All comparison arrivals"}
    results = {}
    summary = json.loads((EXPERIMENT / "summary.json").read_text())["scores"]
    summary_names = {"reference": "reference_median", "fine": "fine_median", "discrete": "discrete_median", "distribution": "distribution_window3"}
    for name in groups["total"].values:
        table = {group: groups[group].records(name, label) for group, label in labels.items()}
        total = table["total"][0]
        model, mode = name.rsplit("_", 1)
        expected = summary[summary_names[model]]["raw" if mode == "raw" else "displayed"]
        if total["arrivals"] != expected["eligible"] or not np.isclose(1-total["miss_rate"], expected["within_5_min"], atol=1e-12, rtol=0) or not np.isclose(total["mae"], expected["mae_scored"], atol=1e-9, rtol=0):
            raise ValueError("diagnostics differ from frozen aggregate scores")
        if model != "reference" and mode == "raw":
            frozen = json.loads((EXPERIMENT / "runs" / model / "result.json").read_text())["later_probe"][TRACKS[model]]
            for group, source in (("day", "by_day"), ("segment", "by_segment")):
                for row in table[group]:
                    key = row["label"] if group == "day" else str(row["key"])
                    prior = frozen[source][key]
                    if row["arrivals"] != prior["eligible"] or not np.isclose(1-row["miss_rate"], prior["within_5_min"], atol=1e-12, rtol=0) or not np.isclose(row["mae"], prior["mae_scored"], atol=1e-9, rtol=0):
                        raise ValueError(f"{model}: saved {group} scores changed")
        station_records = table.pop("station")
        for row in station_records:
            slot = row["key"]
            ident = vocab["station"][slot-1] if slot else None
            detail = stations.get(ident, (None, None, None, None))
            row.update(station_id=ident, state=detail[1], lat=detail[2], lon=detail[3])
        with (OUT / f"{name}_stations.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(station_records[0]))
            writer.writeheader(); writer.writerows(station_records)
        table["stations"] = ranks(station_records)
        table["trains"] = ranks(table.pop("train"), minimum=250)
        table["lines"] = ranks(table.pop("line"))
        results[name] = table
    name = "reference_display"
    joint = groups["station_day"]
    joint_n = joint.n.reshape(len(calendar), station_size)
    joint_m = joint.values[name][0].reshape(len(calendar), station_size)
    total_n, total_m = joint_n.sum(axis=0), joint_m.sum(axis=0)
    spikes = []
    for d, slot in np.argwhere((joint_n >= 100) & ((total_n-joint_n) >= 1000)):
        other_rate = (total_m[slot]-joint_m[d, slot])/(total_n[slot]-joint_n[d, slot])
        spikes.append({"day": labels["day"](int(d)), "station": station_name(int(slot)),
                       "arrivals": int(joint_n[d, slot]), "misses": int(joint_m[d, slot]),
                       "miss_rate": float(joint_m[d, slot]/joint_n[d, slot]), "other_days_miss_rate": float(other_rate),
                       "excess_misses": float(joint_m[d, slot]-joint_n[d, slot]*other_rate)})
    spikes.sort(key=lambda r: -r["excess_misses"])
    with (OUT / "reference_display_station_day.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["service_date", "station_slot", "arrivals", "misses", "miss_rate"])
        for d, slot in np.argwhere(joint_n > 0):
            writer.writerow([labels["day"](int(d)), int(slot), int(joint_n[d, slot]),
                             int(joint_m[d, slot]), float(joint_m[d, slot]/joint_n[d, slot])])
    peak = int((cgroup / "memory.peak").read_text())
    output = {"scope": "Reused validation service dates 2026-09-06..19; training-only reference, not deployed-refit in-sample scoring",
              "miss_definition": "absolute arrival prediction error >5 minutes; displayed uses unchanged app clamp/round rule",
              "rows": n-boundary, "results": results, "station_day_spikes": spikes[:30],
              "resources": {"cgroup_peak_bytes": peak, "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                            "swap_max": (cgroup / "memory.swap.max").read_text().strip()},
              "notes": ["Counts are stop-arrivals, not independent trips", "Dates are service dates; hour is scheduled local time",
                        "Only reference spatial attribution has verified ordered stop IDs; candidate group attribution is exploratory and excluded from published report",
                        "UNKNOWN slots group unseen identities", "Line labels can span several geographic services",
                        "Spikes compare each station with its other thirteen days; service mix can differ; this is not causal attribution"]}
    (OUT / "result.json").write_text(json.dumps(output, indent=2))
    print(json.dumps({"rows": output["rows"], "resources": output["resources"],
                      "totals": {name: table["total"][0] for name, table in results.items()}}, indent=2))


if __name__ == "__main__":
    main()
