"""Predicting one call at request time, against a stub booster.

Mirrors what used to be tested against predict.py's batch job, minus the
database entirely: predict_stop() takes ORM-shaped objects in hand and returns
a value, with no session and no table in between.
"""

from __future__ import annotations

from types import SimpleNamespace

TRAIN = SimpleNamespace(
    id=500, line_id=7, category="RE", operator="DB", segment="RV",
    n_stops=4, first_departure_sec=8 * 3600,
)
STATION = SimpleNamespace(id=1, lat=51.48, lon=7.22)


def test_predicts_from_the_boosters_and_widens_with_the_conformal_offset(stub_ml_model):
    from dbrel.ml.live import predict_stop

    result = predict_stop(day_type="WEEKDAY", train=TRAIN, station=STATION, sched_sec=30_000)

    assert result.p50_dep_delay_s == 120.0
    assert result.conformal_lo_s == 60.0 - 30.0
    assert result.conformal_hi_s == 300.0 + 30.0
    assert result.model_id == "20260101T000000Z"


def test_only_the_requested_call_is_scored_not_the_whole_table(stub_ml_model):
    """The point of going live: one call in, one prediction out."""
    from dbrel.ml.live import predict_stop

    _manifest, boosters = stub_ml_model
    predict_stop(day_type="WEEKDAY", train=TRAIN, station=STATION, sched_sec=30_000)
    assert all(booster.calls == [1] for booster in boosters.values())


def test_predict_many_batches_into_one_booster_call(stub_ml_model):
    """Several rows shown on one page still means one round of inference."""
    from dbrel.ml.live import predict_many

    _manifest, boosters = stub_ml_model
    requests = [
        {"day_type": "WEEKDAY", "train": TRAIN, "station": STATION, "sched_sec": s}
        for s in (30_000, 40_000, 50_000)
    ]
    results = predict_many(requests)
    assert len(results) == 3
    assert all(booster.calls == [3] for booster in boosters.values())


def test_without_an_activated_model_it_declines_rather_than_guesses():
    from dbrel.ml import live

    result = live.predict_stop(day_type="WEEKDAY", train=TRAIN, station=STATION, sched_sec=30_000)
    assert result is None


def test_without_lightgbm_installed_it_declines_rather_than_raises(monkeypatch):
    import builtins

    from dbrel.ml import live

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "lightgbm":
            raise ImportError("blocked for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    result = live.predict_stop(day_type="WEEKDAY", train=TRAIN, station=STATION, sched_sec=30_000)
    assert result is None


def test_the_active_model_is_loaded_only_once_per_process(stub_ml_model, monkeypatch):
    from dbrel.ml import live

    calls = []
    original = live.artifacts.latest

    def counted(root=None):
        calls.append(1)
        return original(root=root)

    monkeypatch.setattr(live.artifacts, "latest", counted)
    live.predict_stop(day_type="WEEKDAY", train=TRAIN, station=STATION, sched_sec=30_000)
    live.predict_stop(day_type="WEEKDAY", train=TRAIN, station=STATION, sched_sec=40_000)
    assert len(calls) == 1
