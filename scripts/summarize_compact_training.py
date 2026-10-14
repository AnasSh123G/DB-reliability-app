"""Summarize frozen validation predictions, including the app's display rule."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dbrel.ml.metrics import score_arrivals
from compact_provenance import verify_training_key, file_hash

WORK = ROOT / "data/ml/compact_training_20261013"
PREPARED = ROOT / "data/ml/full_year/prepared/search-2026-09-26-robust-no-doy-mc20-rd56-eval"
REFERENCE = ROOT / "data/ml/full_year/runs/compact-2026-09-26/trials/f1ac12b1742e3a0b/validation.npz"


def shown(point):
    # delayParts(): <1 minute -> 0, otherwise JavaScript Math.round.
    value = np.asarray(point, dtype=np.float64)
    return np.where(value < 1, 0, np.floor(value+0.5))


def verify_score(actual, frozen, context):
    for metric, value in actual.items():
        expected = frozen[metric]
        if value is None or expected is None:
            same = value is expected
        else:
            same = np.isclose(value, expected, rtol=0, atol=1e-9)
        if not same:
            raise ValueError(f"{context}: saved points differ from frozen metric {metric}")


def verify_groups(y, point, groups, frozen, label):
    actual = {label(value): score_arrivals(y[groups == value], point[groups == value])
              for value in np.unique(groups)}
    if actual.keys() != frozen.keys():
        raise ValueError("saved prediction groups differ from frozen groups")
    for name, score in actual.items():
        verify_score(score, frozen[name], f"group {name}")
    return actual


def daily_delta(y, base, candidate, days):
    records = []
    for day in np.unique(days):
        mask = days == day
        base_score = score_arrivals(y[mask], base[mask])
        candidate_score = score_arrivals(y[mask], candidate[mask])
        records.append({"day": str(np.datetime64(int(day), "D")), "rows": int(mask.sum()),
                        "within5_difference": candidate_score["within_5_min"]-base_score["within_5_min"],
                        "mae_difference": candidate_score["mae_scored"]-base_score["mae_scored"]})
    weight = np.array([d["rows"] for d in records])
    rng = np.random.default_rng(20261013)
    chosen = rng.integers(0, len(records), size=(2000, len(records)))
    out = {"by_day": records, "interpretation": "paired day bootstrap on reused validation; excludes selection uncertainty"}
    for metric in ("within5_difference", "mae_difference"):
        difference = np.array([d[metric] for d in records])
        bootstrap = np.sum(difference[chosen]*weight[chosen], axis=1)/np.sum(weight[chosen], axis=1)
        out[f"{metric}_day_bootstrap_95"] = np.quantile(bootstrap, [0.025, 0.975]).tolist()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--partial", action="store_true", help="include only already completed candidates")
    args = parser.parse_args()
    n = json.loads((PREPARED / "meta.json").read_text())["n"]
    day = np.memmap(PREPARED / "day.bin", dtype="<i4", mode="r", shape=(n,))
    boundary = int(np.searchsorted(day, int(np.datetime64("2026-09-06").astype("datetime64[D]").astype(int))))
    y = np.fromfile(PREPARED / "y.bin", dtype="<f4", offset=boundary*4)
    days = np.asarray(day[boundary:])
    segment = np.fromfile(PREPARED / "segment.bin", dtype="<i2", offset=boundary*2)
    labels = json.loads((ROOT / "data/ml/arrival_full/dictionary.json").read_text())["segment"]
    with np.load(REFERENCE) as z:
        ids = z["stop_id"]
        prepared_ids = np.memmap(PREPARED / "stop_id.bin", dtype="<i8", mode="r", shape=(n,))
        if ids.shape != prepared_ids.shape:
            raise ValueError("reference arrival count differs")
        for start in range(0, n, 262144):
            if not np.array_equal(ids[start:start+262144], prepared_ids[start:start+262144]):
                raise ValueError("reference arrival IDs differ")
        base = z["point"][boundary:]
    if len(base) != len(y):
        raise ValueError("reference rows differ from validation")
    scores = {"reference_median": {"raw": score_arrivals(y, base), "displayed": score_arrivals(y, shown(base)),
              "segments": {label: {"raw": score_arrivals(y[segment == i], base[segment == i]),
                              "displayed": score_arrivals(y[segment == i], shown(base[segment == i]))}
                           for i, label in enumerate(labels)}}}
    models = {}
    missing = []
    for track in ("fine", "wide", "distribution", "discrete"):
        root = WORK / "runs" / track
        path = root / "result.json"
        if not path.exists():
            missing.append(track); continue
        result = json.loads(path.read_text())
        key = json.loads((root / "key.json").read_text())
        training_key = key["source_key"] if track == "discrete" else key
        verify_training_key(training_key, ROOT)
        if result["config"] != training_key["config"] or result["track"] != track:
            raise ValueError(f"{track}: result differs from frozen setup")
        if track == "discrete":
            if file_hash(WORK / "runs/distribution/best.pt") != key["source_weights_sha"]:
                raise ValueError("discrete source weights changed")
            for script, field in (("check_discrete_delay.py", "script_sha"), ("compact_provenance.py", "provenance_sha")):
                if file_hash(ROOT / "scripts" / script) != key[field]:
                    raise ValueError(f"discrete source changed: {script}")
        browser_file = root / "actual_frontend_browser.json"
        models[track] = {"artifact": result.get("artifact"), "resources": result["resources"],
                         "best_epoch": result["best_epoch"], "selection": result["selected_epoch"],
                         "browser": json.loads(browser_file.read_text()) if browser_file.exists() else None}
        for rule in result["later_probe"]:
            point = np.load(root / f"{rule}.npy", mmap_mode="r")
            if point.shape != y.shape:
                raise ValueError(f"{track}/{rule}: prediction count differs")
            name = f"{track}_{rule}"
            raw = score_arrivals(y, point)
            frozen = result["later_probe"][rule]["overall"]
            verify_score(raw, frozen, f"{track}/{rule}")
            verify_groups(y, point, days, result["later_probe"][rule]["by_day"],
                          lambda value: str(np.datetime64(int(value), "D")))
            verify_groups(y, point, segment, result["later_probe"][rule]["by_segment"],
                          lambda value: str(int(value)))
            display = score_arrivals(y, shown(point))
            scores[name] = {"raw": raw, "displayed": display,
                            "segments": {label: {"raw": score_arrivals(y[segment == i], point[segment == i]),
                                           "displayed": score_arrivals(y[segment == i], shown(point[segment == i]))}
                                         for i, label in enumerate(labels)},
                            "raw_daily_difference": daily_delta(y, base, point, days),
                            "raw_improves_precision_and_within5": raw["mae_scored"] <= scores["reference_median"]["raw"]["mae_scored"]
                                 and raw["within_5_min"] > scores["reference_median"]["raw"]["within_5_min"]}
    if missing and not args.partial:
        raise SystemExit(f"training results not complete: {missing}")
    summary = {"scope": "reused later validation, 2026-09-06..19; not an independent test", "rows": len(y),
               "scores": scores, "models": models, "incomplete_tracks": missing,
               "display_rule": "point<1 -> 0; otherwise floor(point+0.5), matching unchanged delayParts.ts"}
    (WORK / "summary.json").write_text(json.dumps(summary, indent=2))
    lines = ["---", "kind: spec", 'title: "Compact delay-model training results"', "---", "",
             "Full-history training authorized on 2026-10-13. All work is inside DB-reliability-app; no commits or promotions.", "",
             "Candidates are trained on 88,948,878 arrivals through 2026-08-22, selected on the first validation fortnight, "
             "and compared below on the identical 4,241,218 arrivals of September 6–19. The original search already inspected "
             "this validation period. These are exploratory validation measurements, not new independent test results.", "",
             "[Training plan](compact-training-plan.md) · [Initial investigation](compact-model-investigation.md) · "
             "[Independent source review](compact-training-review.md) · [Postprocessing review](compact-postprocess-review.md)", "",
             "| Frozen model / decision | Within 5 min | Within 3 min | MAE, min | App display: within 5 | App display: MAE |",
             "|---|---:|---:|---:|---:|---:|"]
    for name, row in scores.items():
        raw, display = row["raw"], row["displayed"]
        lines.append(f"| {name} | {raw['within_5_min']:.2%} | {raw['within_3_min']:.2%} | {raw['mae_scored']:.4f} "
                     f"| {display['within_5_min']:.2%} | {display['mae_scored']:.4f} |")
    lines += ["", "The raw columns use the existing continuous-output scoring rule. Displayed columns apply the app's "
              "unchanged rule: predictions below one minute display zero, and the rest round to whole minutes. "
              "This distinction matters when an output lies close to the edge of the five-minute tolerance.", "",
              "Discrete quantiles use the frozen distribution network, one-minute class labels and a fixed 1e-6 CDF "
              "boundary tolerance. Its entire validation is scored through the exported ONNX CPU graph. The training "
              "head and loss remain binned; metadata separately records the quantile inference transform.", "",
              "| Candidate | Best epoch | Model plus manifest | Strict 35 MB budget | Browser parity | Cgroup peak | GPU allocation peak |",
              "|---|---:|---:|---|---|---:|---:|"]
    artifact_lines = []
    for track, model in models.items():
        artifact, memory = model["artifact"], model["resources"]
        browser = "pending" if model["browser"] is None else ("pass" if model["browser"]["passed"] else "FAIL")
        lines.append(f"| {track} | {model['best_epoch']} | {artifact['download_bytes']/1e6:.3f} MB "
                     f"| {'yes' if artifact['fits_strict_35mb'] else 'no; comparison only'} | {browser} "
                     f"| {memory['cgroup_peak_bytes']/2**30:.3f} GiB | {memory['gpu_peak_allocated_bytes']/2**20:.1f} MiB |")
        artifact_lines.append(f"- `{track}`: `{artifact['path']}`")
    lines += ["", "Offline artifact directories (job swap was disabled):", "", *artifact_lines, ""]
    if missing:
        lines += ["", "Still running or awaiting completion: " + ", ".join(missing) + ".", ""]
    lines += ["", "The quantile head preserves ordered p25/p50/p75. A distribution window point is deliberately different "
              "from its median; gains in a wide tolerance score can come with worse point precision. Quantile diagnostics "
              "and segment/day breakdowns are saved in each result JSON. Inclusive empirical quantile coverage can depart "
              "from nominal percentages because the recorded outcomes contain many minute-valued ties.", "",
              "Paired day-bootstrap intervals in `summary.json` account for variation across these fourteen days but do not "
              "remove selection bias or establish future/general-year performance. No newer complete untouched test week is available.", "",
              "All fits ran serially under MemoryMax=8G, MemoryHigh=5G, MemorySwapMax=0 and four CPU threads. "
              "Epoch checkpoints preserve model, optimizer, schedule and RNG state; CPU interruption/resume tests passed. "
              "99 targeted training, decision-rule and serving tests passed (49 CPU training/postprocessing tests and "
              "50 existing feature/notice-rate/deployment tests); review details are linked above. "
              "GPU crash/resume is not certified by the CPU test. The deployed pointer remains `20261004T213106Z`.", ""]
    (ROOT / "docs/compact-training-results.md").write_text("\n".join(lines))
    print(json.dumps({"completed": list(models), "incomplete": missing, "scores": {
        name: {"within5": row["raw"]["within_5_min"], "mae": row["raw"]["mae_scored"]} for name, row in scores.items()}}, indent=2))


if __name__ == "__main__":
    main()
