"""Serving's trip start time comes from the call's own route variant.

Training reads each run's own start; train.first_departure_sec is only the
first-seen run's, and on another variant it can fall after the call itself.
"""

from __future__ import annotations

from dbrel.ml.timetable import _variant_trip_start
from dbrel.models import StationStop, Train


def _stop(seq: int, dep_min: int | None, arr_min: int | None = None) -> StationStop:
    return StationStop(
        stop_sequence=seq,
        planned_departure_sec=None if dep_min is None else dep_min * 60,
        planned_arrival_sec=None if arr_min is None else arr_min * 60,
    )


def test_the_variants_own_origin_departure_wins():
    train = Train(first_departure_sec=502 * 60)
    assert _variant_trip_start(train, [_stop(1, 480), _stop(2, 489)]) == 480 * 60


def test_stored_start_is_kept_when_it_is_possible():
    train = Train(first_departure_sec=470 * 60)
    assert _variant_trip_start(train, [_stop(3, 489)]) == 470 * 60


def test_stored_start_after_the_first_call_falls_back_to_that_call():
    """The RE 73722 case: stored 08:22, yet the variant calls at 08:09."""
    train = Train(first_departure_sec=502 * 60)
    assert _variant_trip_start(train, [_stop(2, None, arr_min=489)]) == 489 * 60


def test_unknown_stored_start_uses_the_first_call():
    train = Train(first_departure_sec=None)
    assert _variant_trip_start(train, [_stop(4, 600)]) == 600 * 60
