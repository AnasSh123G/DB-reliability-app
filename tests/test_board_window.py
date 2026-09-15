"""The board window is projected from real time onto service days.

Two cases make it non-obvious, and both are silent when wrong: a board rendered
just after midnight, and the two days a year when the local day is not 24 hours.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from dbrel.config import SERVICE_DAY_MAX_S, TZ
from dbrel.web.queries import board_slices

BACK_S = 15 * 60
FWD_S = 90 * 60


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp())


def slices(epoch: int):
    return board_slices(epoch, BACK_S, FWD_S)


def test_midday_window_is_one_service_day():
    out = slices(at(2026, 9, 14, 14, 30))
    assert [d for d, _, _ in out] == [date(2026, 9, 14)]
    _, lo, hi = out[0]
    assert lo == 14 * 3600 + 15 * 60  # 14:15
    assert hi == 16 * 3600  # 16:00


def test_just_after_midnight_keeps_the_previous_service_day():
    """A train departing 23:50 on D-1 is still current at 00:10 on D."""
    out = slices(at(2026, 9, 15, 0, 10))
    days = {d: (lo, hi) for d, lo, hi in out}

    assert date(2026, 9, 14) in days, "the D-1 slice is what stops the board going blank"
    lo, hi = days[date(2026, 9, 14)]
    assert lo == 23 * 3600 + 55 * 60  # 23:55, as seconds from D-1's anchor
    assert hi > 86_400, "the window must reach past midnight in D-1 coordinates"

    assert date(2026, 9, 15) in days
    lo, hi = days[date(2026, 9, 15)]
    assert lo == 0, "the window starts before this service day began; clamp to 0"
    assert hi == 100 * 60  # 01:40


def test_two_days_back_is_never_included():
    out = slices(at(2026, 9, 15, 0, 10))
    assert date(2026, 9, 13) not in {d for d, _, _ in out}


@pytest.mark.parametrize(
    "day,hour",
    [
        (date(2026, 3, 29), 1),  # spring forward: the local day is 23 hours long
        (date(2026, 3, 29), 4),
        (date(2026, 10, 25), 1),  # fall back: 25 hours
        (date(2026, 10, 25), 4),
    ],
)
def test_dst_days_stay_anchored(day, hour):
    """The noon-minus-12h anchor must survive both transitions.

    A naive midnight anchor shifts every time on the board by an hour on these two
    days. The check is that the window's own width is preserved after projection.
    """
    epoch = at(day.year, day.month, day.day, hour)
    out = slices(epoch)
    assert out, "a DST day must still produce a slice"
    for service_date, lo, hi in out:
        assert 0 <= lo <= hi <= SERVICE_DAY_MAX_S
        # Unless clamped at an edge, the projected window keeps its real duration.
        if lo > 0 and hi < SERVICE_DAY_MAX_S:
            assert hi - lo == BACK_S + FWD_S


def test_window_never_exceeds_the_service_day_bound():
    for hour in range(24):
        for service_date, lo, hi in slices(at(2026, 6, 10, hour)):
            assert 0 <= lo <= hi <= SERVICE_DAY_MAX_S
