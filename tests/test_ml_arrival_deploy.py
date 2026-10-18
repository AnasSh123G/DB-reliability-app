"""Serving an arrival-net artifact: artifact/pointer handling, CLI, server input, browser meta, study.

The network is faked (no onnx in the main venv): the fake spreads probability evenly
over the 2..8 min bins, so the window rule gives 5.0 and p25/p50/p75 are 3.5/5.0/6.5.
"""

from __future__ import annotations

import json
import os
import shutil

import numpy as np
import pytest
from typer.testing import CliRunner

from dbrel.holidays_de import load_holiday_dates
from dbrel.ml import features as F
from dbrel.ml.arrival import (
    ARRIVAL_KIND, arrival_inputs, bin_edges, fit_input_prep, promote,
    role_models_dir, save_arrival_artifact,
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


def _quantile_manifest(arrival_artifact):
    saved = json.loads((arrival_artifact / "manifest.json").read_text())
    return {"vocab": saved["vocab"], "prep": saved["prep"], "output": "quantiles", "edges": None,
            "point_rule": "median"}


def test_promote_refuses_comparison_artifacts_even_in_the_served_dir(tabm_artifact, arrival_artifact,  # noqa: F811
                                                                    tmp_path):
    models = arrival_artifact.parent
    pointer = models / CURRENT_NAME
    before = pointer.read_text()
    small = tmp_path / "small.onnx"
    small.write_bytes(b"x" * 1000)
    saved = save_arrival_artifact(role_models_dir("comparison", models), _quantile_manifest(arrival_artifact),
                                  {"model.onnx": small}, "20261002T000000Z", role="comparison",
                                  served_dir=models)
    copied = models / saved.name
    shutil.copytree(saved, copied)  # copied into v2 by hand
    for where in (saved.parent, models):
        with pytest.raises(ValueError, match="comparison artifact"):
            promote(where, saved.name)
        assert pointer.read_text() == before
    # Roleless artifacts (both existing kinds) still count as deployable.
    assert promote(models, arrival_artifact.name) == tabm_artifact.name
    assert promote(models, tabm_artifact.name) == arrival_artifact.name


def _line(output: str, train: str = "RE 10070") -> tuple[str, str]:
    row = next(r for r in output.splitlines() if train in r)
    arr, dep = row.split(" dep ")
    return arr, dep


class FakeQuantileSession(FakeArrivalSession):
    def run(self, outputs, feeds):
        if outputs == ["probs"]:
            return super().run(outputs, feeds)
        assert outputs == ["quantiles"]
        return [np.tile(np.array([[0.5, 1.0, 2.0]], dtype=np.float32), (len(feeds["cat"]), 1))]


def _meta_response(monkeypatch, models_dir, stamp="any"):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dbrel.web.settings")
    import django

    django.setup()
    from django.test import RequestFactory

    from dbrel.web import views

    monkeypatch.setattr(views, "ML_MODELS_DIR", models_dir)
    return views.model_meta(RequestFactory().get(f"/api/model/{stamp}/meta.json"), stamp)


# -- an artifact whose inputs include the notice rates (add-notice-inputs) --------------------------------

def _rates_artifact(env, tmp_path, monkeypatch):  # noqa: F811
    """An arrival artifact trained with the six notice rates; returns (artifact, row inputs, expected)."""
    import onnxruntime

    import dbrel.db as dbmod
    from dbrel.ml.notice_rates import NoticeRates

    ds, enc, vocab = _dataset(env)
    engine = dbmod.make_engine(env / "dbrel.sqlite")
    rates = NoticeRates.from_database(engine, ds.meta["dates"])
    engine.dispose()
    ids = {k: np.array(vocab[name]) for k, name in (("station_id", "station"), ("line_id", "line"),
                                                    ("train_id", "train"))}
    rows = {k: ids[k][enc[name]] for k, name in (("station_id", "station"), ("line_id", "line"),
                                                 ("train_id", "train"))}
    enc = {**enc, **rates.rates(rows["station_id"], rows["line_id"], rows["train_id"],
                                np.zeros(len(rows["train_id"]), dtype=np.int64), fold=False)}
    prep = fit_input_prep(enc, np.ones(len(ds.arr_min), bool), vocab, "robust", numeric=F.NOTICE_NUMERIC)
    manifest = {"vocab": vocab, "prep": prep, "output": "probs", "edges": EDGES.tolist(),
                "point_rule": "window", "notice_rates": rates.record()}
    onnx = tmp_path / "rates.onnx"
    onnx.write_bytes(b"not a real network")
    artifact = save_arrival_artifact(env / "models", manifest, {"model.onnx": onnx}, "20260930T000000Z")
    FakeArrivalSession.calls = []
    monkeypatch.setattr(onnxruntime, "InferenceSession", FakeArrivalSession)
    return artifact, ds, enc, json.loads((artifact / "manifest.json").read_text())


def _train70_row(ds):
    trip = list(PATTERNS).index(70) + 1
    (row,) = np.flatnonzero((ds.trip_id == trip) & (ds.stop_sequence == 20))
    return row


def test_the_server_and_the_offline_path_build_identical_rate_inputs(env, tmp_path, monkeypatch):  # noqa: F811
    import dbrel.db as dbmod
    from dbrel.ml.notice_rates import _serving
    from dbrel.ml.timetable import calls_at
    from dbrel.models import Train
    from dbrel.web.queries import _model_input

    _serving.clear()
    artifact, ds, enc, manifest = _rates_artifact(env, tmp_path, monkeypatch)
    row = _train70_row(ds)
    expected_cat, expected_num = arrival_inputs({k: v[[row]] for k, v in enc.items()}, manifest["prep"])
    station, arr_sec, dep_sec = PATTERNS[70][1]
    with dbmod.session_scope() as s:
        train = s.get(Train, 70)
        web = _model_input(s, manifest, station_id=station, arr_sec=arr_sec, dep_sec=dep_sec, train=train,
                           service_date=START, direction=F.all_directions(s)[70],
                           holiday_set=load_holiday_dates(s))
        call = calls_at(s, train, station, START)[0]
        DelayModel(artifact).predict_calls(s, [call])
    offline_num = FakeArrivalSession.calls[0]["num"][0]
    assert web["cat"] == expected_cat[0].tolist()
    np.testing.assert_allclose(web["num"], expected_num[0], rtol=0, atol=1e-6)
    np.testing.assert_allclose(offline_num, web["num"], rtol=0, atol=1e-6)  # both paths, same array
    n_rates = len(F.NOTICE_RATES)
    assert np.any(np.asarray(web["num"][-n_rates - 3:]) != 0)  # the rate columns are really in the input


def test_changed_counts_make_serving_refuse(env, tmp_path, monkeypatch):  # noqa: F811
    import sqlite3

    import dbrel.db as dbmod
    from dbrel.ml.notice_rates import NoticeRatesMismatch, _serving
    from dbrel.models import Train
    from dbrel.web.queries import _model_input

    _serving.clear()
    _, _, _, manifest = _rates_artifact(env, tmp_path, monkeypatch)
    conn = sqlite3.connect(env / "dbrel.sqlite")
    conn.execute("UPDATE notice_rate_daily SET k_works = k_works + 1 WHERE level = 'station'")
    conn.commit()
    conn.close()
    station, arr_sec, dep_sec = PATTERNS[70][1]
    with dbmod.session_scope() as s, pytest.raises(NoticeRatesMismatch, match="differ from the ones"):
        _model_input(s, manifest, station_id=station, arr_sec=arr_sec, dep_sec=dep_sec,
                     train=s.get(Train, 70), service_date=START, direction=F.all_directions(s)[70],
                     holiday_set=load_holiday_dates(s))


