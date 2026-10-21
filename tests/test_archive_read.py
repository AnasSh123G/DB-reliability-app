"""Reading a partition: URL parsing, the timestamp boundary, and sweep grouping.

These are the pure parts of the archive reader. The parquet path itself is
exercised by the replay tests and by a real day in task 6.1; what matters here is
that the pieces the replay depends on cannot be quietly wrong.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from dbrel.config import TZ
from dbrel.ingest.archive import (
    API_FCHG,
    API_PLAN,
    ArchivedResponse,
    _to_epoch,
    eva_of,
    group_sweeps,
)

P = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/"


# --- task 3.2: the station a request was for ------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (P + "plan/08001506/260828/03", 8001506),
        (P + "fchg/08001506", 8001506),
        # Not every recording is zero padded, and evas are stored unpadded here.
        (P + "fchg/8001506", 8001506),
        (P + "plan/8000105/260828/12", 8000105),
    ],
)
def test_eva_is_recovered_from_either_endpoint(url, expected):
    assert eva_of(url) == expected


# --- task 3.1: the timestamp boundary --------------------------------------


def test_the_spring_forward_and_autumn_back_days_round_trip():
    """Europe/Berlin skips 02:00-03:00 on 2026-03-29 and repeats it on 2026-10-25."""
    spring = _to_epoch(datetime(2026, 3, 29, 3, 30))
    assert datetime.fromtimestamp(spring, TZ).hour == 3

    # The ambiguous hour maps to fold=0, the same convention parser.parse_ts uses.
    autumn = _to_epoch(datetime(2026, 10, 25, 2, 30))
    assert datetime.fromtimestamp(autumn, TZ).hour == 2


# --- task 3.3: sweeps ------------------------------------------------------


def _resp(hour: int, minute: int, eva: int = 8000001, api: str = API_FCHG) -> ArchivedResponse:
    return ArchivedResponse(
        recorded_at=int(datetime(2026, 8, 28, hour, minute, tzinfo=TZ).timestamp()),
        api_name=api,
        eva=eva,
        body=b"<timetable/>",
    )


def test_a_burst_spanning_minutes_stays_one_sweep():
    """Upstream calls thousands of stations per job; that is one pass, not many."""
    responses = [_resp(0, 54 + n, eva=8000000 + n) for n in range(5)]
    assert len(group_sweeps(responses)) == 1


def test_plan_responses_are_not_swept():
    """Only /fchg is a poll; /plan is replayed per hour bucket instead."""
    responses = [_resp(0, 54, api=API_PLAN), _resp(0, 55, api=API_FCHG, eva=8000002)]
    sweeps = group_sweeps(responses)
    assert len(sweeps) == 1
    assert all(r.api_name == API_FCHG for sweep in sweeps for r in sweep)


