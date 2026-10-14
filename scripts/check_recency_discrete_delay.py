"""Interpret a trained one-minute classification head as discrete quantiles.

The frozen validation labels are whole minutes. This candidate takes each
conditional CDF's first class reaching p25/p50/p75 rather than interpolating
uniform density within a bin. It exports the existing quantiles tensor/API.
No training, promotion, database write, or new test-set claim occurs here.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dbrel.ml.arrival import prep_numeric, save_arrival_artifact
from dbrel.ml.arrival_nn import ArrivalNet, NetConfig
from dbrel.ml.features import CATEGORICAL
from dbrel.ml.full_year import PreparedSet
from dbrel.ml.full_year_search import write_browser_check
from train_recency_delay import (
    WORK, EXPERIMENT, PREPARED, PREFIX, TRIAL, Score, Source, PREDICT_ROWS, day_boundary,
    file_hash, guard, notice_record, selection_rank, reference_score, resources, write_json, with_trial_inputs,
)
from recency_experiment import CONDITIONS
from recency_provenance import verify_training_key

# Treat a cumulative mass within one part per million of a quartile as having
# reached it. This is fixed before trained-model evaluation and avoids a
# backend's cumulative rounding selecting the next class at exact CDF ties.
CDF_TOLERANCE = 1e-6


def discrete_quantiles(probs, edges):
    probs = np.asarray(probs, dtype=np.float64)
    if probs.ndim != 2 or probs.shape[1] == 0 or not np.isfinite(probs).all() or np.any(probs < 0):
        raise ValueError("finite nonnegative probability rows with positive mass required")
    with np.errstate(over="ignore"):
        mass = probs.sum(axis=1, keepdims=True)
    if not np.isfinite(mass).all() or np.any(mass <= 0):
        raise ValueError("finite nonnegative probability rows with positive mass required")
    edges = np.asarray(edges, dtype=np.float64)
    if edges.ndim != 1 or not np.isfinite(edges).all() or np.any(np.diff(edges) <= 0):
        raise ValueError("finite strictly increasing bin edges required")
    if len(edges) != probs.shape[1]+1:
        raise ValueError("bin edges differ from probability width")
    cdf = np.cumsum(probs/mass, axis=1)
    index = np.minimum((cdf[:, :, None] < np.array([0.25, 0.5, 0.75])-CDF_TOLERANCE).sum(axis=1), probs.shape[1]-1)
    return np.asarray(edges, dtype=np.float32)[index]


class DiscreteExport(nn.Module):
    def __init__(self, network):
        super().__init__()
        self.network = network.model
        self.register_buffer("labels", torch.tensor(network.edges[:-1], dtype=torch.float32))
        self.register_buffer("quantiles", torch.tensor(np.array([0.25, 0.5, 0.75])-CDF_TOLERANCE, dtype=torch.float64))

    def forward(self, cat, num):
        probs = torch.softmax(self.network(cat, num).float(), dim=-1).double()
        probs = probs/probs.sum(dim=-1, keepdim=True)
        cdf = torch.cumsum(probs, dim=-1)
        index = (cdf.unsqueeze(-1) < self.quantiles).long().sum(dim=1).clamp(max=len(self.labels)-1)
        return self.labels[index]


def score_model(session, data, start, stop, out=None):
    score = Score()
    groups = {"day": {}, "segment": {}}
    points = None if out is None else np.lib.format.open_memmap(out / "median.npy", mode="w+", dtype="float32", shape=(stop-start,))
    source = Source(data, start, stop)
    try:
        for a in range(0, len(source), PREDICT_ROWS):
            b = min(len(source), a+PREDICT_ROWS)
            cats, nums, y = source.read(a, b)
            if np.any(y != np.rint(y)):
                raise ValueError("integer-target assumption is false")
            pct = session.run(["quantiles"], {"cat": cats.astype(np.int64), "num": nums.astype(np.float32)})[0]
            score.add(y, pct[:, 1], pct)
            if points is not None:
                points[a:b] = pct[:, 1]
            for group in groups:
                values = data.column(group)[start+a:start+b]
                for value in np.unique(values):
                    mask = values == value
                    key = str(np.datetime64(int(value), "D")) if group == "day" else str(int(value))
                    groups[group].setdefault(key, Score()).add(y[mask], pct[mask, 1], pct[mask])
            if b % (100*PREDICT_ROWS) == 0:
                print(f"ONNX validation [{start}:{stop}]: {b:,}/{len(source):,}", flush=True)
    finally:
        source.close()
        if points is not None:
            points.flush()
    return {"overall": score.result(), **{f"by_{group}": {key: s.result() for key, s in table.items()}
                                          for group, table in groups.items()}}


def export(network, cats, nums, out):
    import onnx
    import onnxruntime as ort
    module = DiscreteExport(network).cpu().eval()
    cats, nums = cats.astype(np.int64), nums.astype(np.float32)
    c, n = torch.as_tensor(cats), torch.as_tensor(nums)
    with torch.inference_mode():
        expected = module(c, n).numpy()
        # Assert the graph's decision agrees with the scientific reference.
        scientific = discrete_quantiles(network._raw(c, n).numpy(), network.edges)
    if not np.array_equal(expected, scientific):
        raise ValueError("Torch discrete rule differs from NumPy reference")
    out.mkdir()
    torch.onnx.export(module, (c[:64], n[:64]), out / "model.onnx", input_names=["cat", "num"],
                      output_names=["quantiles"], dynamic_axes={"cat": {0: "rows"}, "num": {0: "rows"}, "quantiles": {0: "rows"}},
                      dynamo=False, opset_version=17)
    onnx.checker.check_model(str(out / "model.onnx"))
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(out / "model.onnx"), sess_options=options, providers=["CPUExecutionProvider"])
    actual = session.run(["quantiles"], {"cat": cats, "num": nums})[0]
    difference = float(np.abs(actual-expected).max())
    (out / "prep.json").write_text(json.dumps({"prep": network.prep, "config": network.cfg.as_dict(),
         "output": "quantiles", "edges": None, "postprocess": "discrete conditional quantiles"}, indent=1))
    return {"kind": "onnx", "output": "quantiles", "point_rule": "median", "scaling": network.prep["scaling"],
            "onnx_passed": difference == 0, "onnx_vs_torch_max_abs": difference, "onnx_tolerance": 0,
            "_expected": {"raw": actual.astype(np.float32), "point": actual[:, 1].astype(np.float32), "pct": actual.astype(np.float32)}}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--condition',default='weighted',choices=tuple(CONDITIONS))
    args = parser.parse_args()
    global WORK
    WORK = EXPERIMENT / args.condition
    import onnx
    import onnxruntime as ort
    cg = guard()
    started = time.monotonic()
    source = WORK / "runs/distribution"
    completed = json.loads((source / "result.json").read_text())
    source_key = json.loads((source / "key.json").read_text())
    inputs = verify_training_key(source_key, ROOT)
    if completed["config"] != source_key["config"] or completed["track"] != "distribution":
        raise ValueError("source result differs from its frozen training setup")
    cfg = NetConfig(**completed["config"])
    if cfg.head != "binned":
        raise ValueError("discrete CDF rule requires a binned source network")
    net = ArrivalNet(cfg, inputs["prep"])
    if file_hash(source / 'history.json') != completed['history_sha256']:
        raise ValueError('classifier selection history differs from completed training')
    epochs = json.loads((source / 'history.json').read_text())['epochs']
    chosen = max(epochs, key=lambda e: selection_rank(e['selection_views']['discrete']))
    discrete_epoch = chosen['epoch']
    checkpoint = source / f'epoch-{discrete_epoch:03d}.pt'
    if file_hash(checkpoint) != chosen['checkpoint_sha256']:
        raise ValueError('selected discrete checkpoint differs from its training-time identity')
    weights = torch.load(source / f'epoch-{discrete_epoch:03d}.pt', map_location='cpu', weights_only=True)
    net.model.load_state_dict(weights)
    del weights
    torch.set_num_threads(4)
    root = WORK / "runs/discrete"
    root.mkdir(exist_ok=True)
    key = {"source_weights_sha": file_hash(source / f"epoch-{discrete_epoch:03d}.pt"), "selected_epoch": discrete_epoch, "source_key": source_key,
           "script_sha": file_hash(Path(__file__)), "provenance_sha": file_hash(ROOT / "scripts/recency_provenance.py"),
           "torch_version": str(torch.__version__), "cdf_tolerance": CDF_TOLERANCE,
           "onnx_version": onnx.__version__, "onnxruntime_version": ort.__version__, "numpy_version": np.__version__,
           "postprocess": "discrete conditional p25/p50/p75"}
    if (root / "key.json").exists() and json.loads((root / "key.json").read_text()) != key:
        raise ValueError("discrete setup changed; preserve this run and use a fresh directory")
    if (root / "result.json").exists():
        saved = json.loads((root / 'result.json').read_text())
        if saved['selected_epoch'] != discrete_epoch or not saved['onnx']['onnx_passed']:
            raise ValueError('completed discrete result differs from selected checkpoint')
        identity = saved['prediction_identity']
        for name, expected in {**identity['arrays'], 'stop_ids.npy':identity['stop_ids_sha256']}.items():
            if file_hash(root / name) != expected:
                raise ValueError('completed discrete prediction content changed')
        for name, expected in saved['export_identity'].items():
            if file_hash(ROOT / name) != expected:
                raise ValueError('completed discrete export content changed')
        print('Completed discrete export verified; no writes', flush=True)
        return
    write_json(root / "key.json", key)
    val, fit = PreparedSet(PREPARED / f"{PREFIX}eval"), PreparedSet(PREPARED / f"{PREFIX}fit")
    val = with_trial_inputs(val, inputs["prep"])
    boundary = day_boundary(val)
    index = np.linspace(0, boundary-1, 512, dtype=int)
    cats = np.concatenate([val.cats[int(i):int(i)+1] for i in index])
    nums = np.concatenate([val.nums[int(i):int(i)+1] for i in index])
    cats, nums = np.concatenate([cats, np.zeros_like(cats[:64])]), np.concatenate([nums, nums[:64]])
    if (root / "onnx").exists():
        (root / "onnx").rename(root / f"onnx-previous-{time.time_ns()}")
    check = export(net, cats, nums, root / "onnx")
    if not check["onnx_passed"]:
        write_json(root / "export_failure.json", {k: v for k, v in check.items() if k != "_expected"})
        raise ValueError("discrete ONNX parity failed")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(root / "onnx/model.onnx"), sess_options=options, providers=["CPUExecutionProvider"])
    selection = score_model(session, val, 0, boundary)
    probe = score_model(session, val, boundary, len(val), root)
    result = {"scope": "existing validation, not fresh test", "track": "discrete", "config": cfg.as_dict(),
              "best_epoch": discrete_epoch, "selected_epoch": discrete_epoch, "selected_metrics": selection["overall"],
              "selection_policy": "same shared classifier fit; discrete within5 ranking on selection dates only",
              "metric_runtime": f"ONNX Runtime CPU {ort.__version__}; full frozen validation through exported graph",
              "reference": reference_score(val, boundary), "later_probe": {"median": probe}}
    write_json(root / "metrics.json", result)
    result["onnx"] = {k: v for k, v in check.items() if k != "_expected"}
    if not check["onnx_passed"]:
        raise ValueError("discrete ONNX parity failed")
    result["onnx_bytes"] = (root / "onnx/model.onnx").stat().st_size
    # The underlying network trained a binned loss; its public output is now
    # quantiles, so metadata must match that existing frontend branch.
    write_browser_check(root / "browser_check", "discrete", check, root / "onnx", cats, nums)
    manifest = {"recency": completed["recency"], "label_policy": fit.key["label_correction"]["policy"], "source": {"experiment": str(root.relative_to(ROOT)), "weights": str(source.relative_to(ROOT))},
                "config": cfg.as_dict(), "prep": inputs["prep"], "vocab": inputs["vocab"],
                "output": "quantiles", "edges": None, "point_rule": "median",
                "postprocess": {"kind": "discrete conditional quantiles", "target_resolution_min": 1.0,
                                "cdf_boundary_tolerance": CDF_TOLERANCE,
                                "end_bins": "training labels outside [-30,120) use endpoint classes"},
                "features": list(CATEGORICAL)+list(prep_numeric(inputs["prep"])), "notice_rates": notice_record(fit),
                "scores": {"selection": selection["overall"], "later_validation": probe}, "onnx_check": result["onnx"],
                "disclosure": ["existing validation reused; not an independent test", "derived from frozen distribution weights",
                               "minute-valued targets; integer CDF quantiles replace within-bin interpolation",
                               "CDF ties use a fixed 1e-6 probability tolerance", "historical five-hour-cutoff replay",
                               "present-day timetable patterns; historical timetable-as-of not verified"]}
    models = root / "artifacts"
    models.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifact = save_arrival_artifact(models, manifest, {"model.onnx": root / "onnx/model.onnx"}, stamp,
                                     role="deployable", served_dir=ROOT / "models/v2")
    measured = json.loads((artifact / "manifest.json").read_text())["download_bytes"]
    result["artifact"] = {"path": str(artifact.relative_to(ROOT)), "download_bytes": measured,
                          "role": "deployable", "fits_strict_35mb": measured <= 35_000_000}
    if file_hash(source / 'stop_ids.npy') != completed['prediction_identity']['stop_ids_sha256']:
        raise ValueError('producer stop-ID content changed')
    ordered = np.load(source / 'stop_ids.npy', mmap_mode='r')
    if ordered.shape != (len(val)-boundary,):
        raise ValueError('producer stop-ID shape differs from prepared comparison')
    for begin in range(boundary,len(val),262144):
        end = min(begin+262144,len(val))
        if not np.array_equal(ordered[begin-boundary:end-boundary],val.column('stop_id')[begin:end]):
            raise ValueError('producer ordered stop IDs differ from prepared rows')
    np.save(root / 'stop_ids.npy', ordered)
    result['prediction_identity'] = {'stop_ids_sha256': file_hash(root / 'stop_ids.npy'),
        'arrays': {p.name:file_hash(p) for p in root.glob('*.npy') if p.name!='stop_ids.npy'}}
    result['recency'] = completed['recency']
    result['export_identity'] = {str(path.relative_to(ROOT)): file_hash(path) for path in
        (root/'onnx/model.onnx', root/'onnx/prep.json', artifact/'model.onnx', artifact/'manifest.json')}
    result["resources"] = resources(cg, time.monotonic()-started)
    write_json(root / "result.json", result)
    print(json.dumps({"selection": selection["overall"], "probe": probe["overall"], "artifact": result["artifact"]}, indent=2))


if __name__ == "__main__":
    main()
