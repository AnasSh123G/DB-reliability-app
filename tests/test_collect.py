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
    buckets_by_date,
    day_needs_backfill,
    exclusive_lock,
    hour_buckets,
    plan_window,
)
from dbrel.config import TZ
from dbrel.db import Base
from dbrel.models import Station, TtPlanFetch

# --------------------------------------------------------------------------
# parse_duration / format_duration
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# hour_buckets -- the DST cases
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# next_midnight_after -- --start-at-midnight
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# plan_window -- the watermark-trap fix itself
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# apply_station_seed
# --------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


# --------------------------------------------------------------------------
# day_needs_backfill -- the gate on the once-a-day full-day /plan sweep
# --------------------------------------------------------------------------


def _attempt(session, eva: int, day: str, hour: int, status: int = 200) -> None:
    session.add(
        TtPlanFetch(eva=eva, plan_date=day, plan_hour=hour, fetched_at=0, http_status=status)
    )


def _track(session, *evas: int) -> None:
    for index, eva in enumerate(evas, start=1):
        session.add(
            Station(id=index, natural_key=f"s{index}@1,1", name=f"S{index}",
                    lat=1, lon=1, eva=eva, tracked=1)
        )
    session.flush()


def test_day_needs_backfill_is_false_once_every_bucket_was_attempted(session):
    _track(session, 8000041)
    for hour in range(24):
        _attempt(session, 8000041, "2026-09-18", hour)
    session.flush()
    assert day_needs_backfill(session, "2026-09-18") is False


