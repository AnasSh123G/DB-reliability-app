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
    SWEEP_GAP_S,
    ArchivedResponse,
    _to_epoch,
    eva_of,
    group_sweeps,
    plan_date_hour_of,
    sweep_instant,
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


@pytest.mark.parametrize(
    "url",
    [
        P + "station/BLS",
        "https://apis.deutschebahn.com/db-api-marketplace/apis/station-data/v2/stations",
        "https://apis.deutschebahn.com/db-api-marketplace/apis/fasta/v2/facilities",
        P + "fchg/not-a-number",
        "",
    ],
)
def test_urls_that_name_no_station_are_rejected_not_guessed(url):
    """The mirror also records station-data and fasta calls."""
    assert eva_of(url) is None


def test_plan_date_and_hour_come_from_the_url():
    """The payload never says which hour it is for; only the request did."""
    assert plan_date_hour_of(P + "plan/08001506/260828/03") == ("260828", 3)
    assert plan_date_hour_of(P + "plan/08001506/260828/00") == ("260828", 0)
    assert plan_date_hour_of(P + "plan/08001506/260828/23") == ("260828", 23)


@pytest.mark.parametrize(
    "url",
    [P + "fchg/08001506", P + "plan/08001506", P + "plan/08001506/260828", P + "plan/x/y/z"],
)
def test_a_url_without_a_date_and_hour_yields_none(url):
    assert plan_date_hour_of(url) is None


# --- task 3.1: the timestamp boundary --------------------------------------


def test_a_naive_partition_timestamp_is_read_as_berlin_local():
    """The mirror applies no timezone conversion; the boundary must."""
    naive = datetime(2026, 8, 28, 0, 57, 20)
    assert _to_epoch(naive) == int(datetime(2026, 8, 28, 0, 57, 20, tzinfo=TZ).timestamp())


def test_summer_and_winter_offsets_both_resolve():
    """CEST in August (+2), CET in January (+1). A fixed offset would break one."""
    summer = _to_epoch(datetime(2026, 8, 28, 12, 0))
    winter = _to_epoch(datetime(2026, 1, 28, 12, 0))
    assert datetime.fromtimestamp(summer, TZ).hour == 12
    assert datetime.fromtimestamp(winter, TZ).hour == 12
    # Same wall clock, different offset, so not 212 whole days apart to the second.
    assert (winter - summer) % 3600 == 0


def test_the_spring_forward_and_autumn_back_days_round_trip():
    """Europe/Berlin skips 02:00-03:00 on 2026-03-29 and repeats it on 2026-10-25."""
    spring = _to_epoch(datetime(2026, 3, 29, 3, 30))
    assert datetime.fromtimestamp(spring, TZ).hour == 3

    # The ambiguous hour maps to fold=0, the same convention parser.parse_ts uses.
    autumn = _to_epoch(datetime(2026, 10, 25, 2, 30))
    assert datetime.fromtimestamp(autumn, TZ).hour == 2


def test_an_iso_string_timestamp_is_accepted_too():
    assert _to_epoch("2026-08-28 00:57:20") == _to_epoch(datetime(2026, 8, 28, 0, 57, 20))


def test_a_missing_timestamp_is_none_not_an_epoch_of_zero():
    assert _to_epoch(None) is None


# --- task 3.3: sweeps ------------------------------------------------------


def _resp(hour: int, minute: int, eva: int = 8000001, api: str = API_FCHG) -> ArchivedResponse:
    return ArchivedResponse(
        recorded_at=int(datetime(2026, 8, 28, hour, minute, tzinfo=TZ).timestamp()),
        api_name=api,
        eva=eva,
        body=b"<timetable/>",
    )


def test_the_real_aug_28_sweeps_are_recovered():
    """Measured on the live partition: bursts at 00:54-00:58 and 15:20-15:25."""
    responses = (
        [_resp(0, 54 + n % 5, eva=8000000 + n) for n in range(40)]
        + [_resp(15, 20 + n % 6, eva=8000000 + n) for n in range(40)]
    )
    sweeps = group_sweeps(responses)
    assert len(sweeps) == 2, "two bursts ~14h apart are two sweeps"
    first, second = sweeps
    assert datetime.fromtimestamp(sweep_instant(first), TZ).hour == 0
    assert datetime.fromtimestamp(sweep_instant(second), TZ).hour == 15


def test_a_burst_spanning_minutes_stays_one_sweep():
    """Upstream calls thousands of stations per job; that is one pass, not many."""
    responses = [_resp(0, 54 + n, eva=8000000 + n) for n in range(5)]
    assert len(group_sweeps(responses)) == 1


def test_sweeps_are_split_on_a_gap_well_under_the_six_hour_cadence():
    just_over = SWEEP_GAP_S // 60 + 1
    responses = [_resp(3, 0), _resp(3, just_over, eva=8000002)]
    assert len(group_sweeps(responses)) == 2


def test_plan_responses_are_not_swept():
    """Only /fchg is a poll; /plan is replayed per hour bucket instead."""
    responses = [_resp(0, 54, api=API_PLAN), _resp(0, 55, api=API_FCHG, eva=8000002)]
    sweeps = group_sweeps(responses)
    assert len(sweeps) == 1
    assert all(r.api_name == API_FCHG for sweep in sweeps for r in sweep)


def test_the_sweep_instant_is_the_median_not_the_extreme():
    """One straggling station must not drag the reference past an event boundary."""
    responses = [_resp(3, 0, eva=1), _resp(3, 1, eva=2), _resp(3, 2, eva=3)]
    assert datetime.fromtimestamp(sweep_instant(responses), TZ).minute == 1


def test_sweeps_come_back_in_chronological_order():
    """Replay order decides which reading supersedes which."""
    responses = [_resp(15, 20, eva=1), _resp(0, 54, eva=2), _resp(9, 30, eva=3)]
    sweeps = group_sweeps(responses)
    instants = [sweep_instant(s) for s in sweeps]
    assert instants == sorted(instants)


def test_no_changes_means_no_sweeps():
    assert group_sweeps([]) == []
    assert group_sweeps([_resp(0, 54, api=API_PLAN)]) == []
