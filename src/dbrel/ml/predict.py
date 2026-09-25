"""Predict a scheduled call's delay from the saved model and the timetable alone.

Reads the artifact `CURRENT` points at, and -- through `timetable.calls_at` --
the `train`, `station_stop`, `station` and holiday tables. Never `trip_stop`,
`trip`, or either averages table: a prediction must work on a machine whose
averages were never built.

A call with no scheduled departure (the train terminates here) gets no
departure prediction, and none is derived from its arrival; a call with no
scheduled arrival (it starts here) likewise gets no arrival prediction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

from dbrel.holidays_de import load_holiday_dates
from dbrel.ml.bakeoff import MANIFEST_NAME, current_artifact
from dbrel.ml.candidates import LIBRARIES
from dbrel.ml.features import (
    RAW_COLUMNS,
    compute_features,
    direction_for,
    encode_categoricals,
    feature_matrix,
)
from dbrel.ml.nn import KIND as TABM_KIND
from dbrel.ml.nn import TabMPredictor
from dbrel.ml.timetable import ScheduledCall


class NoModelError(RuntimeError):
    pass


@dataclass
class Prediction:
    call: ScheduledCall
    arr: tuple[float, float, float] | None  # p25, p50, p75 minutes
    dep: tuple[float, float, float] | None


class DelayModel:
    def __init__(self, artifact: Path):
        self.artifact = artifact
        self.manifest = json.loads((artifact / MANIFEST_NAME).read_text())
        self.models = {}
        self.tabm = None
        if self.manifest.get("kind") == TABM_KIND:
            self.tabm = TabMPredictor(artifact, self.manifest)
            return
        for side, library in self.manifest["winners"].items():
            meta = self.manifest["candidates"][library]["sides"][side]["fit"]
            self.models[side] = LIBRARIES[library].load(artifact / side, meta)

    @classmethod
    def current(cls, models_dir: Path) -> DelayModel:
        artifact = current_artifact(models_dir)
        if artifact is None:
            raise NoModelError(
                f"no trained delay model under {models_dir} -- export the TabM model with "
                "`data/build/torch-rocm/venv/bin/python scripts/export_tabm.py` first"
            )
        return cls(artifact)

    def predict_calls(self, session: Session, calls: list[ScheduledCall], *,
                      direction=direction_for) -> list[Prediction]:
        """`direction(session, train)` defaults to `direction_for`; a caller predicting many
        trains can pass a cached equivalent (web.queries._ModelDirectionCache.get), which
        computes each line's split once instead of once per train."""
        if not calls:
            return []
        holiday_set = load_holiday_dates(session)
        directions: dict[int, int] = {}
        raw: dict[str, list] = {name: [] for name in RAW_COLUMNS}
        for call in calls:
            if call.train.id not in directions:
                directions[call.train.id] = direction(session, call.train)
            row = call.raw(direction=directions[call.train.id], holiday_set=holiday_set)
            for name in RAW_COLUMNS:
                raw[name].extend(row[name])
        arrays = {name: np.array(values, dtype=object if name in ("category", "operator", "state")
                                 else np.float64) for name, values in raw.items()}
        features = encode_categoricals(compute_features(arrays), self.manifest["vocab"])
        if self.tabm is not None:
            preds = self.tabm.predict(features)
        else:
            X = feature_matrix(features)
            preds = {side: model.predict(X)[0] for side, model in self.models.items()}
        out = []
        for i, call in enumerate(calls):
            arr = tuple(float(v) for v in preds["arr"][i]) if call.planned_arrival_sec is not None else None
            dep = tuple(float(v) for v in preds["dep"][i]) if call.planned_departure_sec is not None else None
            out.append(Prediction(call=call, arr=arr, dep=dep))
        return out
