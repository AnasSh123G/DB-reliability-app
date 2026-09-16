"""Pure-function tests for dbrel.collect: no network, no DB except the seed test.

hour_buckets is the one that matters most: it is the fix for the watermark trap
in plan_import._pending_hours (see collect.py's module docstring and
docs/probe-findings.md §24), so its DST behaviour is pinned here rather than
trusted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.collect import (
    AlreadyRunning,
    SeedStation,
    apply_station_seed,
    buckets_by_date,
    exclusive_lock,
    format_duration,
    hour_buckets,
    parse_duration,
    plan_window,
)
from dbrel.config import TZ
from dbrel.db import Base
from dbrel.models import Station

# --------------------------------------------------------------------------
# parse_duration / format_duration
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("4h", 14400),
        ("7d", 604800),
        ("90m", 5400),
        ("45s", 45),
        ("1h30m", 5400),
        ("2d12h", 216000),
        ("300", 300),
        (" 4H ", 14400),
        ("1W", 604800),
    ],
)
def test_parse_duration_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "4x", "-1h", "1.5h", "h", "4h30", "  "])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_format_duration_roundtrips_common_values():
    assert format_duration(14400) == "4h"
    assert format_duration(5400) == "1h30m"
    assert format_duration(0) == "0s"


# --------------------------------------------------------------------------
# hour_buckets -- the DST cases
# --------------------------------------------------------------------------


def test_hour_buckets_ordinary_case():
    start = datetime(2026, 9, 15, 10, 0, tzinfo=TZ)
    end = datetime(2026, 9, 15, 12, 30, tzinfo=TZ)
    assert hour_buckets(start, end) == [
        ("2026-09-15", 10),
        ("2026-09-15", 11),
        ("2026-09-15", 12),
    ]


def test_hour_buckets_degenerate_start_equals_end():
    instant = datetime(2026, 9, 15, 10, 30, tzinfo=TZ)
    assert hour_buckets(instant, instant) == [("2026-09-15", 10)]


def test_hour_buckets_midnight_crossing_splits_by_date_ascending():
    start = datetime(2026, 9, 15, 22, 10, tzinfo=TZ)
    end = datetime(2026, 9, 16, 2, 0, tzinfo=TZ)
    buckets = hour_buckets(start, end)
    assert buckets == [
        ("2026-09-15", 22),
        ("2026-09-15", 23),
        ("2026-09-16", 0),
        ("2026-09-16", 1),
        ("2026-09-16", 2),
    ]
    assert buckets_by_date(buckets) == [("2026-09-15", [22, 23]), ("2026-09-16", [0, 1, 2])]


def test_hour_buckets_spring_forward_skips_the_missing_hour():
    """2026-03-29: 02:00 CET -> 03:00 CEST. Local hour 2 never exists, so /plan
    must never be asked for it."""
    start = datetime(2026, 3, 29, 0, 30, tzinfo=TZ)
    end = datetime(2026, 3, 29, 5, 30, tzinfo=TZ)
    buckets = hour_buckets(start, end)
    hours = [h for _, h in buckets]
    assert hours == [0, 1, 3, 4, 5]


def test_hour_buckets_fall_back_requests_the_doubled_hour_once():
    """2026-10-25: 03:00 CEST -> 02:00 CET. Local hour 1 occurs twice in real
    time, but /plan has exactly one bucket labelled hour 1 -- requesting it
    twice would be a wasted call, not a correctness issue, but the dedup
    should still collapse it."""
    start = datetime(2026, 10, 25, 0, 30, tzinfo=TZ)
    end = datetime(2026, 10, 25, 5, 30, tzinfo=TZ)
    buckets = hour_buckets(start, end)
    hours = [h for _, h in buckets]
    assert hours == [0, 1, 2, 3, 4, 5]
    assert len(hours) == len(set(hours)), "no hour should be requested twice"


def test_hour_buckets_requires_aware_datetimes():
    with pytest.raises(ValueError):
        hour_buckets(datetime(2026, 9, 15), datetime(2026, 9, 15, 1))


# --------------------------------------------------------------------------
# plan_window -- the watermark-trap fix itself
# --------------------------------------------------------------------------


def test_plan_window_clamps_to_max_lookback_when_previous_is_ancient():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=TZ)
    previous = now - timedelta(days=3)
    start, end = plan_window(previous, now, lead_hours=3, max_lookback_s=16 * 3600)
    assert start == now - timedelta(hours=16)
    assert end == now + timedelta(hours=3)


def test_plan_window_applies_overlap_when_previous_is_recent():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=TZ)
    previous = now - timedelta(hours=4)
    start, end = plan_window(
        previous, now, lead_hours=3, max_lookback_s=16 * 3600, overlap_s=3600
    )
    assert start == previous - timedelta(hours=1)
    assert end == now + timedelta(hours=3)


def test_plan_window_with_no_history_reaches_the_full_lookback():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=TZ)
    start, end = plan_window(None, now, lead_hours=3, max_lookback_s=16 * 3600)
    assert start == now - timedelta(hours=16)


def test_plan_window_4h_cycle_covers_every_hour_the_naive_watermark_would_skip():
    """This is the assertion that directly encodes the bug being fixed: at a 4h
    cadence, _pending_hours's 2h lookback clamp would silently drop the hours
    between (previous+1h) and (now-2h). plan_window + hour_buckets must not."""
    now = datetime(2026, 9, 16, 12, 0, tzinfo=TZ)
    previous = now - timedelta(hours=4)
    start, end = plan_window(previous, now, lead_hours=3, max_lookback_s=16 * 3600)
    buckets = hour_buckets(start, end)
    covered = {(day, hour) for day, hour in buckets}
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor <= end:
        assert (cursor.date().isoformat(), cursor.hour) in covered, f"gap at {cursor}"
        cursor += timedelta(hours=1)


# --------------------------------------------------------------------------
# exclusive_lock
# --------------------------------------------------------------------------


def test_exclusive_lock_refuses_a_second_holder(tmp_path):
    lock_path = tmp_path / "sweep.lock"
    with exclusive_lock(lock_path, label="first"):
        with pytest.raises(AlreadyRunning):
            with exclusive_lock(lock_path, label="second"):
                pass


def test_exclusive_lock_is_reusable_after_release(tmp_path):
    lock_path = tmp_path / "sweep.lock"
    with exclusive_lock(lock_path, label="first"):
        pass
    with exclusive_lock(lock_path, label="second"):
        pass  # must not raise


# --------------------------------------------------------------------------
# apply_station_seed
# --------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


def test_apply_station_seed_matches_by_natural_key_over_name(session):
    session.add_all(
        [
            Station(id=1, natural_key="bochum hbf@51.479,7.223", name="Bochum Hbf", lat=51.479, lon=7.223),
            Station(id=2, natural_key="bochum hbf@50.000,7.223", name="Bochum Hbf", lat=50.0, lon=7.223),
        ]
    )
    session.flush()
    seed = [SeedStation(name="Bochum Hbf", eva=8000041, ds100="EBO", natural_key="bochum hbf@51.479,7.223")]

    result = apply_station_seed(session, seed)

    assert result.applied == ["Bochum Hbf"]
    assert result.tracked == 1
    matched = session.get(Station, 1)
    other = session.get(Station, 2)
    assert matched.eva == 8000041
    assert matched.eva_source == "seed"
    assert matched.tracked == 1
    assert other.eva is None, "the wrong same-named station must not be touched"


def test_apply_station_seed_ambiguous_name_with_no_natural_key_is_skipped(session):
    session.add_all(
        [
            Station(id=1, natural_key="a@1,1", name="Duplicate Hbf", lat=1, lon=1),
            Station(id=2, natural_key="b@2,2", name="Duplicate Hbf", lat=2, lon=2),
        ]
    )
    session.flush()
    seed = [SeedStation(name="Duplicate Hbf", eva=8000041, ds100=None, natural_key=None)]

    result = apply_station_seed(session, seed)

    assert result.applied == []
    assert len(result.ambiguous) == 1
    assert session.get(Station, 1).eva is None
    assert session.get(Station, 2).eva is None


def test_apply_station_seed_missing_station_is_reported(session):
    seed = [SeedStation(name="Nowhere Hbf", eva=8000041, ds100=None, natural_key=None)]
    result = apply_station_seed(session, seed)
    assert result.missing == ["Nowhere Hbf"]
    assert result.applied == []


def test_apply_station_seed_conflict_never_overwrites(session):
    session.add(Station(id=1, natural_key="x@1,1", name="X Hbf", lat=1, lon=1, eva=9999999))
    session.flush()
    seed = [SeedStation(name="X Hbf", eva=8000041, ds100=None, natural_key=None)]

    result = apply_station_seed(session, seed)

    assert len(result.conflicts) == 1
    assert session.get(Station, 1).eva == 9999999


def test_apply_station_seed_rejects_eva_outside_rail_range(session):
    session.add(Station(id=1, natural_key="x@1,1", name="X Hbf", lat=1, lon=1))
    session.flush()
    seed = [SeedStation(name="X Hbf", eva=212342, ds100=None, natural_key=None)]  # a real bus-stop eva, §14

    result = apply_station_seed(session, seed)

    assert len(result.rejected) == 1
    assert session.get(Station, 1).eva is None


def test_apply_station_seed_happy_path_sets_every_field(session):
    session.add(Station(id=1, natural_key="x@1,1", name="X Hbf", lat=1, lon=1))
    session.flush()
    seed = [SeedStation(name="X Hbf", eva=8000041, ds100="EBO", natural_key=None)]

    apply_station_seed(session, seed, priority=2)

    station = session.get(Station, 1)
    assert station.eva == 8000041
    assert station.ds100 == "EBO"
    assert station.eva_source == "seed"
    assert station.tracked == 1
    assert station.poll_priority == 2


def test_default_station_seed_file_is_well_formed():
    from dbrel.config import DEFAULT_STATIONS_FILE
    from dbrel.collect import load_station_seed

    seed = load_station_seed(DEFAULT_STATIONS_FILE)
    assert len(seed) == 15
    evas = [s.eva for s in seed]
    assert len(evas) == len(set(evas)), "duplicate eva in the seed file"
    natural_keys = [s.natural_key for s in seed if s.natural_key]
    assert len(natural_keys) == len(set(natural_keys))
    from dbrel.config import RAIL_EVA_RANGE

    for s in seed:
        assert s.eva in RAIL_EVA_RANGE, f"{s.name}: eva {s.eva} outside RAIL_EVA_RANGE"
