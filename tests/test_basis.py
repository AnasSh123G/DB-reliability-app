"""_average's provisional marking.

Shown from the first sample rather than hidden below AVG_MIN_SAMPLES: hiding it
would mean every board reads "not enough observations" for roughly the first five
service days. "provisional" marks it below the threshold instead, so a reader is
never misled into treating one run as settled.
"""

from __future__ import annotations

from dbrel.config import AVG_MIN_SAMPLES
from dbrel.models import TrainStationDelay
from dbrel.web.queries import _average


def _row(**overrides):
    defaults = dict(
        station_id=1, train_id=1, dow=0,
        n_arr=1, n_dep=1, n_runs=1, n_known=1, n_cancelled=0,
        avg_dep_delay_s=60.0, avg_arr_delay_s=60.0,
        p50_dep_delay_s=60, p90_dep_delay_s=60,
        p50_arr_delay_s=60, p90_arr_delay_s=60,
        pct_dep_punctual=1.0, pct_arr_punctual=1.0,
        computed_at=1, rule_version=1,
    )
    defaults.update(overrides)
    return TrainStationDelay(**defaults)


def test_average_shows_a_figure_from_a_single_sample():
    row = _row(n_dep=1)
    avg = _average(row, "dep")
    assert avg is not None
    assert avg["n"] == 1
    assert avg["provisional"] is True


def test_average_is_not_provisional_at_the_sample_floor():
    row = _row(n_dep=AVG_MIN_SAMPLES)
    avg = _average(row, "dep")
    assert avg["provisional"] is False


def test_average_is_none_only_with_zero_samples():
    row = _row(n_dep=0, avg_dep_delay_s=None)
    assert _average(row, "dep") is None


def test_average_is_none_for_a_missing_row():
    assert _average(None, "dep") is None
