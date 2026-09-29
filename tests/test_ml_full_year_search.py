"""The full-year search end to end on the synthetic fixture (torch; skipped outside the ROCm venv).

    SP=$(data/build/torch-rocm/venv/bin/python -c "import site;print(site.getsitepackages()[0])")
    PYTHONPATH=src:$SP uv run --offline python -m pytest -q -p no:cacheprovider tests/test_ml_full_year_search.py
"""

from __future__ import annotations

import json
import sqlite3

import pytest

pytest.importorskip("torch")
pytest.importorskip("tabm")
pytest.importorskip("onnxruntime")

from dbrel.ml import full_year_search as S  # noqa: E402
from dbrel.ml.arrival import COMPARISON_DIR_NAME, promote  # noqa: E402
from dbrel.ml.full_history import build_full_history  # noqa: E402
from dbrel.ml.memcheck import gate  # noqa: E402
from dbrel.ml.split import TrainPruning, load_inspected  # noqa: E402

from test_ml_full_history import _directions, db  # noqa: E402, F401 - fixture

SMALL = dict(embedding_max_dim=4, train_emb_dim=4, batch_size=64, epochs=3, patience=3, lr=1e-2)
KEEP_ALL = TrainPruning(min_count=1)
SPLIT = dict(test_days=2, val_days=2, min_train_days=2)


def small(arch, head, hidden=16, blocks=1, k=2):
    params = S.net(arch, head, hidden, blocks, 4, k=k, pruning=KEEP_ALL)
    return {**params, "config": {**params["config"], **SMALL}}


OVERSIZED = S.net("mlp", "quantile", 8192, 2, 4, pruning=KEEP_ALL)  # ~134M parameters, estimated only
COMPACT = S.Track("compact", "deployable", 35_000_000,
                  (small("mlp", "quantile"), small("mlp", "binned")), variant="ablation")
LARGE = S.Track("large", "comparison", 100_000_000, (small("tabm", "quantile"), OVERSIZED),
                variant="loose-pruning")
HIST = S.Track("hist", "deployable", 35_000_000, ({**S.histogram(1), "pruning": KEEP_ALL.as_dict()},),
               variant="ablation")


@pytest.fixture
def run(db, tmp_path):  # noqa: F811
    conn = sqlite3.connect(db)
    conn.execute("UPDATE trip SET finalized_at = CAST(strftime('%s', service_date) AS INTEGER) + 86400")
    conn.commit()
    conn.close()
    build_full_history(db, tmp_path / "full", _directions(db))
    models = tmp_path / "models" / "v2"
    models.mkdir(parents=True)
    return tmp_path, models


def test_search_final_and_export_end_to_end(run, monkeypatch):
    tmp, models = run
    work, inspected = tmp / "work", tmp / "inspected_dates.json"
    s = S.setup(tmp / "full", work, [], **SPLIT)
    assert len(s.split.test) == 2 and len(s.split.validation) == 2 and len(s.split.train) >= 2

    compact = S.search_track(s, COMPACT, device="cpu", models_dir=models)
    assert len(compact["trials"]) == 3 and not compact["skipped"] and not compact["not_run"]
    ablation = [t for t in compact["trials"] if t["params"]["numeric"] == "no-doy"]
    assert len(ablation) == 1 and ablation[0]["params"]["family"] == "net"
    assert all(t["validation"]["coverage"] == 1.0 and t["asset_basis"] == "estimate" for t in compact["trials"])
    assert set(compact["references"]) == set(S.REFERENCES)
    assert all("missing" in r for r in compact["references"].values())
    exp = S.ArrivalExperiment(work / "runs", f"compact-{s.split.test[-1]}",
                              S.experiment_key(s.history, s.split, s.cutoff, COMPACT))
    exp.open()
    saved = json.loads((exp.dir / "ranking.json").read_text())
    assert saved["chosen"] == compact["chosen"] == S.choose_winner(exp.completed())
    assert S.search_track(s, COMPACT, device="cpu", models_dir=models)["trials"] == compact["trials"]  # resumed

    large = S.search_track(s, LARGE, device="cpu", models_dir=models)
    [skip] = large["skipped"]
    assert skip["params"] == OVERSIZED and skip["estimate_bytes"] > 100_000_000
    assert "100,000,000-byte budget" in skip["reason"]
    assert [t["params"]["pruning"]["min_count"] for t in large["trials"]].count(10) == 1  # the variant ran
    hist = S.search_track(s, HIST, device="cpu", models_dir=models)
    assert hist["trials"][0]["asset_basis"] == "measured"

    sizes = (s.history.rows_per_date[s.split.train[0]], sum(s.history.rows_per_date[d] for d in s.split.train))
    check = S.memory_check(tmp / "full", work, COMPACT, inspected=[], split_args=SPLIT, device="cpu",
                           models_dir=models, sizes=sizes, log=lambda m: None)
    assert [p["killed"] for p in check["probes"]] == [False, False]
    assert check["projected_peak"] > 0 and check["projected_final_peak"] >= check["projected_peak"]
    step = ["python", "scripts/search_full_year.py", "search", "--track", "compact", "--work", str(work)]
    gate([step], "64G", tmp / "ml")

    result = S.final(s, [COMPACT, LARGE, HIST], device="cpu", models_dir=models, inspected_path=inspected,
                     log=lambda m: None)
    final_dir = work / "final" / s.split.test[-1]
    assert (final_dir / "final.json").exists() and result["test_dates"] == list(s.split.test)
    assert set(result["scores"]) == {"compact-winner", "large-winner", "hist-winner"}
    assert all(list(scores["by_month"]) == ["2026-09"] for scores in result["scores"].values())
    assert result["missing_references"].keys() == S.REFERENCES.keys()
    assert load_inspected(inspected) == list(s.split.test)
    with pytest.raises(ValueError, match="uninspected"):
        S.setup(tmp / "full", work, load_inspected(inspected), **SPLIT)  # the test week is spent
    with pytest.raises(FileExistsError):
        S.final(s, [COMPACT], device="cpu", models_dir=models, inspected_path=inspected)

    deployable = S.export_final(final_dir, COMPACT, models_dir=models, log=lambda m: None)
    assert deployable.parent == models
    manifest = json.loads((deployable / "manifest.json").read_text())
    assert manifest["role"] == "deployable" and manifest["download_bytes"] <= 35_000_000
    assert manifest["onnx_check"]["onnx_passed"] and manifest["prep"]["features_numeric"][-2:] == ["doy_sin", "doy_cos"]
    check_dir = final_dir / "compact" / "browser_check"
    assert set(json.loads((check_dir / "manifest.json").read_text())["candidates"]) == {"compact"}
    reference = json.loads((check_dir / "reference" / "reference.json").read_text())
    assert reference["rows"] == result["rows"] and set(reference["expected"]["compact"]) == {"raw", "point", "pct"}
    assert (check_dir / "compact" / "model.onnx").read_bytes() == (deployable / "model.onnx").read_bytes()
    comparison = S.export_final(final_dir, LARGE, models_dir=models, log=lambda m: None)
    assert comparison.parent == models.parent / COMPARISON_DIR_NAME
    assert json.loads((comparison / "manifest.json").read_text())["role"] == "comparison"
    with pytest.raises(ValueError, match="never served"):
        promote(comparison.parent, comparison.name)
    with pytest.raises(ValueError, match="only network winners"):
        S.export_final(final_dir, HIST, models_dir=models)

    # a saved artifact as a reference: re-scored on the same validation rows as the trials
    monkeypatch.setattr(S, "REFERENCES", {"exported-compact": deployable.name})
    again = S.search_track(S.setup(tmp / "full", tmp / "work2", [], **SPLIT), HIST, device="cpu",
                           models_dir=models)
    ref = again["references"]["exported-compact"]
    assert ref["artifact"] == str(deployable) and ref["validation"]["coverage"] == 1.0
    assert ref["validation"]["eligible"] == again["trials"][0]["validation"]["eligible"]


def _trial(mae, coverage, size):
    return {"params": {}, "seconds": 1.0,
            "summary": {"validation": {"mae_scored": mae, "coverage": coverage}, "asset_bytes": size}}


def test_winner_is_the_smallest_full_coverage_trial_close_to_the_best():
    trials = {"best": _trial(1.00, 1.0, 30_000_000), "close-small": _trial(1.015, 1.0, 5_000_000),
              "far-smaller": _trial(1.10, 1.0, 1_000_000), "partial": _trial(0.90, 0.95, 100)}
    assert S.choose_winner(trials) == "close-small"
    assert S.choose_winner({"best": trials["best"], "far-smaller": trials["far-smaller"]}) == "best"
