"""Select a versioned offline experiment without leaving repository data/ml."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_RELATIVE = Path('data/ml/recency_training_20261013')
TRIALS = Path('data/ml/full_year/runs/compact-2026-09-26/trials')
NO_DOY_TRIAL = TRIALS / 'f1ac12b1742e3a0b'
SEASONAL_TRIAL = TRIALS / '634b5e057ab5fe00'  # no-doy numerics plus doy_sin/doy_cos
# condition -> (last-month recency amplitude, input trial relative to the repository)
CONDITIONS = {'control': (0.0, NO_DOY_TRIAL), 'weighted': (0.5, NO_DOY_TRIAL),
              'strong': (2.0, NO_DOY_TRIAL), 'seasonal': (0.0, SEASONAL_TRIAL)}
DEFAULT_CONDITIONS = ('control', 'weighted')


def directory(root):
    root = Path(root).resolve()
    requested = Path(os.environ.get('DBREL_RECENCY_EXPERIMENT', str(DEFAULT_RELATIVE)))
    path = (requested if requested.is_absolute() else root/requested).resolve()
    if root not in path.parents or (root/'data/ml').resolve() not in path.parents:
        raise ValueError('experiment directory must be inside repository data/ml')
    return path


def conditions(experiment):
    """readiness.json's `conditions`, control first; older experiments ran control/weighted."""
    readiness = Path(experiment) / 'readiness.json'
    names = tuple(json.loads(readiness.read_text()).get('conditions', DEFAULT_CONDITIONS)) \
        if readiness.is_file() else DEFAULT_CONDITIONS
    if len(names) < 2 or names[0] != 'control' or len(set(names)) != len(names) or not set(names) <= set(CONDITIONS):
        raise ValueError('experiment conditions must be control first, then known variants')
    return names
