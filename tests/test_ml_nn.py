"""The TabM model's input preparation and its use by predict-delay (the network itself is faked)."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
from typer.testing import CliRunner

from dbrel.cli import app
from dbrel.ml.bakeoff import CURRENT_NAME
from dbrel.ml.dataset import load_dataset
from dbrel.ml.features import CATEGORICAL, NUMERIC, UNKNOWN
from dbrel.ml.nn import INPUT_CAT, INPUT_NUM, model_inputs, numeric_stats, save_artifact
from dbrel.ml.split import masks_for, restrict_to_training_vocab, split_dates

from test_ml_bakeoff import env  # noqa: F401 - the fixture database and dataset

runner = CliRunner()


def _trained_names(with_missing=()):
    names = []
    for name in NUMERIC:
        names.append(name)
        if name in with_missing:
            names.append(f"{name}_missing")
        if name in ("sched_min", "trip_start_min"):
            names += [f"{name}_sin", f"{name}_cos"]
    return names


def _encoded(n=3):
    out = {c: np.array([UNKNOWN, 0, 5][:n], dtype=np.int32) for c in CATEGORICAL}
    for name in NUMERIC:
        out[name] = np.array([0.0, 10.0, np.nan][:n])
    return out


def test_inputs_shift_categoricals_and_standardise_like_training():
    stats = {name: [10.0, 2.0] for name in NUMERIC}
    cards = [4] * len(CATEGORICAL)
    cats, nums = model_inputs(_encoded(), cards=cards, numeric=_trained_names(("lat",)), stats=stats)
    assert cats.dtype == np.int64 and nums.dtype == np.float32
    assert cats[:, 0].tolist() == [0, 1, 3]  # UNKNOWN -> 0, code 0 -> 1, beyond the table -> clipped
    names = _trained_names(("lat",))
    col = {n: nums[:, i] for i, n in enumerate(names)}
    assert col["is_holiday"].tolist() == [-5.0, 0.0, 0.0]  # (0-10)/2, (10-10)/2, NaN -> 0
    assert col["lat_missing"].tolist() == [0.0, 0.0, 1.0]
    assert col["sched_min_sin"][1] == pytest.approx(math.sin(2 * math.pi * 10 / 1440))
    assert col["sched_min_cos"][2] == pytest.approx(1.0)  # NaN time -> angle 0


def test_inputs_refuse_a_column_list_that_does_not_match_the_model():
    stats = {name: [0.0, 1.0] for name in NUMERIC}
    with pytest.raises(ValueError, match="do not match"):
        model_inputs(_encoded(), cards=[9] * len(CATEGORICAL), numeric=_trained_names()[:-1], stats=stats)


class FakeSession:
    """Stands in for onnxruntime: median = 1 + first numeric input, band +-0.5 / +1."""

    calls = []

    def __init__(self, path, providers=None):
        FakeSession.path = path

    def run(self, outputs, feeds):
        FakeSession.calls.append(feeds)
        base = 1.0 + feeds[INPUT_NUM][:, :1]
        side = np.concatenate([base - 0.5, base, base + 1.0], axis=1)
        return [np.stack([side, side + 10.0], axis=1).astype(np.float32)]  # dep, arr


@pytest.fixture
def tabm_artifact(env, tmp_path, monkeypatch):  # noqa: F811
    import onnxruntime

    ds = load_dataset(env / "ml")
    split = split_dates(ds.meta["rows_per_date"])
    masks = masks_for(ds.day, ds.meta["dates"], split)
    features, vocab = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    manifest = {"vocab": vocab, "cards": [len(vocab[c]) + 1 for c in CATEGORICAL],
                "numeric": _trained_names(), "stats": numeric_stats(features, masks["train"])}
    onnx = tmp_path / "net.onnx"
    onnx.write_bytes(b"not a real network")
    artifact = save_artifact(env / "models", manifest, onnx, "20260924T000000Z")
    FakeSession.calls = []
    monkeypatch.setattr(onnxruntime, "InferenceSession", FakeSession)
    return artifact


def test_artifact_names_match_the_bake_off():
    from dbrel.ml import bakeoff, nn

    assert (nn.CURRENT_NAME, nn.MANIFEST_NAME) == (bakeoff.CURRENT_NAME, bakeoff.MANIFEST_NAME)


def test_artifact_is_complete_and_current(tabm_artifact):
    models = tabm_artifact.parent
    assert (models / CURRENT_NAME).read_text().strip() == tabm_artifact.name
    manifest = json.loads((tabm_artifact / "manifest.json").read_text())
    assert manifest["kind"] == "tabm" and (tabm_artifact / manifest["model_file"]).exists()
    assert not list(models.glob(".*.partial"))
    with pytest.raises(FileExistsError):
        save_artifact(models, manifest, tabm_artifact / "model.onnx", tabm_artifact.name)


def test_predict_delay_runs_the_tabm_model(tabm_artifact):
    result = runner.invoke(app, ["ml", "predict-delay", "--train", "RE 10070",
                                 "--station", "Dortmund Hbf", "--date", "2026-09-20"])
    assert result.exit_code == 0, result.output
    assert FakeSession.path.endswith("model.onnx")
    feeds = FakeSession.calls[0]
    assert feeds[INPUT_CAT].dtype == np.int64 and feeds[INPUT_CAT].shape[1] == len(CATEGORICAL)
    assert feeds[INPUT_NUM].shape[1] == len(_trained_names())
    line = next(row for row in result.output.splitlines() if "RE 10070" in row)
    arr_part, dep_part = line.split(" dep ")
    arr = float(arr_part.split("min (")[0].split()[-1])
    dep = float(dep_part.split("min (")[0].split()[-1])
    assert arr == pytest.approx(dep + 10.0, abs=0.1)  # output side 1 is arrival, side 0 departure


def test_terminating_call_still_has_no_departure_with_tabm(tabm_artifact):
    result = runner.invoke(app, ["ml", "predict-delay", "--train", "RE 10070",
                                 "--station", "Hamm(Westf)Hbf", "--date", "2026-09-20"])
    assert result.exit_code == 0, result.output
    line = next(row for row in result.output.splitlines() if "RE 10070" in row)
    arr_part, dep_part = line.split(" dep ")
    assert "min (" in arr_part and "min (" not in dep_part
