"""Serving an arrival-net artifact: artifact/pointer handling, CLI, server input, browser meta, study.

The network is faked (no onnx in the main venv): the fake spreads probability evenly
over the 2..8 min bins, so the window rule gives 5.0 and p25/p50/p75 are 3.5/5.0/6.5.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
from typer.testing import CliRunner

from dbrel.cli import app
from dbrel.holidays_de import load_holiday_dates
from dbrel.ml import features as F
from dbrel.ml.arrival import (
    ARRIVAL_KIND, arrival_inputs, bin_edges, browser_meta, fit_input_prep, promote,
    save_arrival_artifact,
)
from dbrel.ml.dataset import build_arrival_dataset, load_arrival_dataset
from dbrel.ml.nn import CURRENT_NAME
from dbrel.ml.predict import DelayModel
from dbrel.ml.split import restrict_to_training_vocab

from ml_fixture import PATTERNS, START
from test_ml_bakeoff import env  # noqa: F401 - the fixture database and dataset
from test_ml_nn import tabm_artifact  # noqa: F401 - a v2 artifact (fake ONNX)

runner = CliRunner()
EDGES = bin_edges(-5, 20)
STAMP = "20260926T000000Z"


class FakeArrivalSession:
    calls: list = []

    def __init__(self, path, providers=None):
        FakeArrivalSession.path = path

    def run(self, outputs, feeds):
        assert outputs == ["probs"]
        FakeArrivalSession.calls.append(feeds)
        probs = np.zeros((len(feeds["cat"]), len(EDGES) - 1), dtype=np.float32)
        probs[:, 7:13] = 1 / 6  # bins [2, 8)
        return [probs]


def _dataset(env):  # noqa: F811
    import dbrel.db as dbmod

    with dbmod.session_scope() as s:
        build_arrival_dataset(env / "dbrel.sqlite", env / "ml", F.all_directions(s))
    ds = load_arrival_dataset(env / "ml")
    enc, vocab = restrict_to_training_vocab(ds.features, ds.meta["vocab"], np.ones(len(ds.arr_min), bool))
    return ds, enc, vocab


@pytest.fixture
def arrival_artifact(env, tmp_path, monkeypatch):  # noqa: F811
    import onnxruntime

    ds, enc, vocab = _dataset(env)
    prep = fit_input_prep(enc, np.ones(len(ds.arr_min), bool), vocab, "robust")
    manifest = {"vocab": vocab, "prep": prep, "output": "probs", "edges": EDGES.tolist(),
                "point_rule": "window"}
    onnx = tmp_path / "net.onnx"
    onnx.write_bytes(b"not a real network")
    artifact = save_arrival_artifact(env / "models", manifest, {"model.onnx": onnx}, STAMP)
    FakeArrivalSession.calls = []
    monkeypatch.setattr(onnxruntime, "InferenceSession", FakeArrivalSession)
    return artifact


def test_saving_never_moves_current_and_promote_rolls_back(tabm_artifact, arrival_artifact):  # noqa: F811
    models = arrival_artifact.parent
    pointer = models / CURRENT_NAME
    assert pointer.read_text().strip() == tabm_artifact.name  # saving did not move it
    manifest = json.loads((arrival_artifact / "manifest.json").read_text())
    assert manifest["kind"] == ARRIVAL_KIND and manifest["sides"] == ["arr"]
    assert manifest["asset_bytes"] == (arrival_artifact / "model.onnx").stat().st_size

    assert promote(models, arrival_artifact.name) == tabm_artifact.name
    assert pointer.read_text().strip() == arrival_artifact.name
    assert promote(models, tabm_artifact.name) == arrival_artifact.name  # rollback
    assert pointer.read_text().strip() == tabm_artifact.name
    assert not list(models.glob("*.tmp")) and not list(models.glob(".*.partial"))

    (models / "incomplete").mkdir()
    with pytest.raises(FileNotFoundError):
        promote(models, "incomplete")
    assert pointer.read_text().strip() == tabm_artifact.name
    with pytest.raises(ValueError, match="incomplete"):
        save_arrival_artifact(models, {"vocab": {}}, {"model.onnx": arrival_artifact / "model.onnx"}, "x")
    with pytest.raises(FileExistsError):
        save_arrival_artifact(models, manifest, {"model.onnx": arrival_artifact / "model.onnx"}, STAMP)


def _line(output: str, train: str = "RE 10070") -> tuple[str, str]:
    row = next(r for r in output.splitlines() if train in r)
    arr, dep = row.split(" dep ")
    return arr, dep


def test_predict_delay_prints_arrival_only(arrival_artifact):
    promote(arrival_artifact.parent, arrival_artifact.name)
    result = runner.invoke(app, ["ml", "predict-delay", "--train", "RE 10070",
                                 "--station", "Dortmund Hbf", "--date", START.isoformat()])
    assert result.exit_code == 0, result.output
    assert "arrival only" in result.output and "not the median" in result.output
    arr, dep = _line(result.output)
    assert "+5.0 min (+3.5 … +6.5)" in arr
    assert dep.split()[-1] == "–"  # departures are never predicted by this artifact
    feeds = FakeArrivalSession.calls[0]
    manifest = json.loads((arrival_artifact / "manifest.json").read_text())
    assert feeds["cat"].shape[1] == len(F.CATEGORICAL)
    assert feeds["num"].shape[1] == len(manifest["prep"]["numeric"])


def test_origin_call_has_no_arrival_prediction(arrival_artifact):
    promote(arrival_artifact.parent, arrival_artifact.name)
    result = runner.invoke(app, ["ml", "predict-delay", "--train", "RE 10070",
                                 "--station", "Köln Hbf", "--date", START.isoformat()])
    assert result.exit_code == 0, result.output
    arr, dep = _line(result.output)
    assert "min (" not in arr and "min (" not in dep


def test_v2_artifact_still_predicts_both_sides(tabm_artifact, arrival_artifact):  # noqa: F811
    model = DelayModel(tabm_artifact)
    assert model.arrival is None and model.tabm is not None


def test_server_input_uses_the_arrival_clock_and_skips_origins(env, arrival_artifact):  # noqa: F811
    import dbrel.db as dbmod
    from dbrel.models import Train
    from dbrel.web.queries import _model_input

    manifest = json.loads((arrival_artifact / "manifest.json").read_text())
    ds, enc, _ = _dataset(env)
    station, arr_sec, dep_sec = PATTERNS[70][1]
    trip = list(PATTERNS).index(70) + 1  # train 70 on the first date
    (row,) = np.flatnonzero((ds.trip_id == trip) & (ds.stop_sequence == 20))
    expected_cat, expected_num = arrival_inputs({k: v[[row]] for k, v in enc.items()}, manifest["prep"])
    with dbmod.session_scope() as s:
        train = s.get(Train, 70)
        kwargs = dict(train=train, service_date=START, direction=F.all_directions(s)[70],
                      holiday_set=load_holiday_dates(s))
        got = _model_input(s, manifest, station_id=station, arr_sec=arr_sec, dep_sec=dep_sec, **kwargs)
        origin, _, origin_dep = PATTERNS[70][0]
        assert _model_input(s, manifest, station_id=origin, arr_sec=None, dep_sec=origin_dep,
                            **kwargs) is None
    assert got["cat"] == expected_cat[0].tolist()
    np.testing.assert_allclose(got["num"], expected_num[0], rtol=0, atol=1e-6)


def _meta_response(monkeypatch, models_dir, stamp="any"):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dbrel.web.settings")
    import django

    django.setup()
    from django.test import RequestFactory

    from dbrel.web import views

    monkeypatch.setattr(views, "ML_MODELS_DIR", models_dir)
    return views.model_meta(RequestFactory().get(f"/api/model/{stamp}/meta.json"), stamp)


def test_browser_meta_exposes_only_what_inference_needs(tabm_artifact, arrival_artifact, monkeypatch):  # noqa: F811
    models = arrival_artifact.parent
    v2 = json.loads(_meta_response(monkeypatch, models).content)
    assert v2 == {"kind": "tabm", "output": "quantiles", "sides": ["dep", "arr"], "point_rule": "median"}
    promote(models, arrival_artifact.name)
    meta = json.loads(_meta_response(monkeypatch, models).content)
    assert meta["kind"] == ARRIVAL_KIND and meta["output"] == "probs" and meta["point_rule"] == "window"
    assert meta["sides"] == ["arr"] and meta["edges"] == EDGES.tolist()
    assert not {"vocab", "prep", "stats"} & meta.keys()
    assert meta == browser_meta(json.loads((arrival_artifact / "manifest.json").read_text()))


def test_browser_meta_without_any_model_is_404(tmp_path, monkeypatch):
    assert _meta_response(monkeypatch, tmp_path / "empty").status_code == 404


def test_live_study_with_arrival_net_reports_arrivals_only(env, arrival_artifact):  # noqa: F811
    import dbrel.db as dbmod
    from dbrel.models import Station
    from dbrel.study.live_accuracy import (
        RealizedSide, StudyResult, render_report, resolve_call, save_report, score_comparisons,
        score_stops,
    )
    from test_live_accuracy import _event, _stop

    station_id, arr_sec, dep_sec = PATTERNS[70][1]
    promote(arrival_artifact.parent, arrival_artifact.name)
    model = DelayModel.current(arrival_artifact.parent)
    with dbmod.session_scope() as s:
        station = s.get(Station, station_id)
        arr_event, dep_event = _event(arr_sec, delay_s=240), _event(dep_sec, delay_s=180)
        stop = _stop(arr=arr_event, dep=dep_event)
        sides = [RealizedSide(station=station, stop=stop, side="arr", event=arr_event, real_delay_min=4.0),
                 RealizedSide(station=station, stop=stop, side="dep", event=dep_event, real_delay_min=3.0)]
        comparisons = score_stops(s, model, [resolve_call(s, side) for side in sides], now=0)
    arr, dep = sorted(comparisons, key=lambda c: c.side)
    assert arr.side == "arr" and arr.tabm == pytest.approx((3.5, 5.0, 6.5))
    assert dep.side == "dep" and dep.tabm is None
    scores = score_comparisons(comparisons)
    assert scores["dep"]["n"] == 0 and scores["arr"]["n"] == 1
    result = StudyResult(stations_sampled=1, stations_skipped=0, skip_reasons={},
                         stops_skipped_no_local_match=0, scores=scores,
                         n_scored={k: v["n"] for k, v in scores.items()}, comparisons=comparisons,
                         model_kind=model.manifest["kind"], point_rule=model.manifest["point_rule"])
    report = render_report(result, created_at="20260926T120000Z")
    assert "departures are not predicted" in report and "Not predicted" in report
    assert "not a median" in report and "TabM" not in report
    out = save_report(result, env / "studies", stamp="20260926T120000Z")
    header = (out / "comparisons.csv").read_text().splitlines()[0]
    assert "model_point" in header and "tabm_p50" not in header
    assert "Model estimate (window rule)" in (out / "histogram.html").read_text()
