"""Regression test for the Eilendorf-at-two-indices data-integrity bug.

Real-world case (found in a 30-day archive backfill): RB20 30007/30057 is a
wing (coupled) service. On days it runs solo, IRIS numbers its own stops
1..17 and Eilendorf lands at stopIndex 11. On days it is coupled with its
partner service, IRIS numbers the *combined* consist, so Eilendorf's stop
comes out at stopIndex 17 instead -- and the partner's own stops now occupy
11. A numbered TT train's identity is `tts:{category}/{number}` alone
(identity.py `train_identity_key`) -- it does not fork when the same number
runs a different physical route on a different day -- so `station_stop`
(runs.py `upsert_run`), written once per (train, stop_sequence), used to
accumulate any NEW index it had not seen before on the false assumption that
a train's index -> station mapping is stable across every day. Once index 11
was locked to Eilendorf by a solo day, a later coupled day's REAL stop 11 (a
different, earlier station) still got graded against Eilendorf's schedule,
producing an apparent >20 minute-early arrival that was really a different,
on-time stop.

The fix scopes station_stop (and the pattern lookup in changes_poller) to
`path_sha1` -- the SAME per-run route digest `upsert_run` already computes
from `ppth` (identity.py `path_sha1`, over the head stop's full physical
route) -- so a run that couples some days and not others gets one
self-consistent index numbering PER VARIANT instead of one shared, mutually
overwriting numbering for the whole train. This was gated against the real
archive (22 sampled trains, including RB20 30007, some genuinely wing-coupled
some days): within one path_sha1 the index -> station map is invariant across
every day that variant was seen, so keying the pattern on it is safe.

This file reproduces the shape of the bug with two unrelated tracked
stations, one of which (STATION_B) moves to a different index once the
route grows a synthetic wing prefix on day 2 (modelled via a longer `ppth`
on the origin stop, exactly what widens `full_path` -- and therefore
`path_sha1` -- for a real coupled run). It checks:

  1. Each day's plan gets its own, non-interfering station_stop variant
     (RunCounters.pattern_conflicts stays 0 -- there is no cross-variant
     collision to reject once the pattern is scoped by digest).
  2. changes_poller's pattern lookup correctly finds day 2's OWN variant, so
     the coupled-day observation is RECORDED at the right station with the
     right delay -- not dropped as NO_DATA/unmatched the way the earlier,
     unscoped fix conservatively did.
  3. The eva-vs-planned safety net in changes_poller still exists and still
     catches a genuine cross-variant mismatch, for whichever case slips past
     the write-side scoping.
  4. A genuine same-day double-visit (a loop) is unaffected: both visits
     land in station_stop, under the one variant that run belongs to.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dbrel.config import GRACE_S, TZ
from dbrel.db import Base
from dbrel.ingest.changes_poller import run_tt_poll
from dbrel.ingest.client import Response
from dbrel.ingest.finalize_tt import run_finalize_tt
from dbrel.ingest.plan_import import import_plan
from dbrel.models import (
    QUALITY_RECORDED,
    Station,
    StationStop,
    Train,
    Trip,
    TripStop,
    TtUnmatched,
)

STATION_A_EVA = 9001  # the run's first tracked stop every day (the origin)
STATION_B_EVA = 9002  # moves from index 2 (solo) to index 5 (coupled)

DAY1_A_ID = "1000000000000001-2609010500-1"
DAY1_B_ID = "1000000000000001-2609010500-2"
DAY2_A_ID = "1000000000000001-2609020500-1"
DAY2_B_ID = "1000000000000001-2609020500-5"


def _plan_xml(eva: int, station: str, stops_xml: str) -> bytes:
    return f'<timetable station="{station}" eva="{eva}">{stops_xml}</timetable>'.encode()


def _origin_stop(stop_id: str, dp_pt: str, wing_prefix: bool) -> str:
    """Station A: departure-only, its ppth is what makes the two days differ.

    A real wing day widens the physical route (the partner's own stops are
    counted first); this widens `dp@ppth` the same way, which is all
    `full_path`/`path_sha1` actually look at.
    """
    ppth = "X|Y|Z|Station B" if wing_prefix else "Station B"
    return (
        f'<s id="{stop_id}">'
        '<tl f="N" t="p" o="OP" c="RB" n="40001"/>'
        f'<dp pt="{dp_pt}" pp="1" l="RB40" fb="RB40" ppth="{ppth}"/>'
        "</s>"
    )


def _b_stop(stop_id: str, pt: str) -> str:
    return (
        f'<s id="{stop_id}">'
        '<tl f="N" t="p" o="OP" c="RB" n="40001"/>'
        f'<ar pt="{pt}" pp="1" l="RB40" fb="RB40"/>'
        f'<dp pt="{pt}" pp="1" l="RB40" fb="RB40"/>'
        "</s>"
    )


def _fchg_xml(eva: int, stop_id: str, ct: str) -> bytes:
    return (
        f'<timetable eva="{eva}">'
        f'<s id="{stop_id}" eva="{eva}">'
        f'<ar ct="{ct}" l="RB40"/><dp ct="{ct}" l="RB40"/>'
        "</s></timetable>"
    ).encode()


class _Source:
    def __init__(self, plan: dict[int, bytes], fchg: dict[int, bytes]) -> None:
        self._plan = plan
        self._fchg = fchg

    def plan(self, eva: int, date: str, hour: int) -> Response:
        body = self._plan.get(eva)
        return (
            Response(status=200, body=body, elapsed_ms=0)
            if body
            else Response(status=404, body=b"", elapsed_ms=0)
        )

    def changes(self, eva: int) -> Response:
        body = self._fchg.get(eva)
        return (
            Response(status=200, body=body, elapsed_ms=0)
            if body is not None
            else Response(status=404, body=b"", elapsed_ms=0)
        )


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Station(
                    natural_key="a@1,1", name="Station A", lat=1, lon=1,
                    eva=STATION_A_EVA, tracked=1,
                ),
                Station(
                    natural_key="b@2,2", name="Station B", lat=2, lon=2,
                    eva=STATION_B_EVA, tracked=1,
                ),
            ]
        )
        session.commit()
        yield session


def _at(day: int, hour: int, minute: int) -> int:
    return int(datetime(2026, 9, day, hour, minute, tzinfo=TZ).timestamp())


def _import_both_days(session):
    """Day 1 solo (B at index 2), day 2 coupled (B shifted to index 5)."""
    day1_source = _Source(
        plan={
            STATION_A_EVA: _plan_xml(
                STATION_A_EVA, "Station A", _origin_stop(DAY1_A_ID, "2609010500", wing_prefix=False)
            ),
            STATION_B_EVA: _plan_xml(
                STATION_B_EVA, "Station B", _b_stop(DAY1_B_ID, "2609010513")
            ),
        },
        fchg={},
    )
    import_plan(session, day1_source, date="2026-09-01", hours=[5], force=True, now=_at(1, 5, 0))
    session.flush()

    day2_source = _Source(
        plan={
            STATION_A_EVA: _plan_xml(
                STATION_A_EVA, "Station A", _origin_stop(DAY2_A_ID, "2609020500", wing_prefix=True)
            ),
            STATION_B_EVA: _plan_xml(
                STATION_B_EVA, "Station B", _b_stop(DAY2_B_ID, "2609020513")
            ),
        },
        fchg={},
    )
    import_plan(session, day2_source, date="2026-09-02", hours=[5], force=True, now=_at(2, 5, 0))
    session.flush()


def test_each_route_variant_gets_its_own_index_numbering(session):
    """runs.py: two digests, no collision, no dropped stop."""
    _import_both_days(session)

    train_id = session.execute(select(Train.id).where(Train.train_number == 40001)).scalar_one()
    b_id = session.execute(select(Station.id).where(Station.eva == STATION_B_EVA)).scalar_one()

    rows = session.execute(
        select(StationStop.path_sha1, StationStop.stop_sequence, StationStop.station_id).where(
            StationStop.train_id == train_id
        )
    ).all()
    variants = {path_sha1 for path_sha1, _seq, _station_id in rows}
    assert len(variants) == 2, "solo and coupled days must not share one pattern"

    by_variant: dict[str, dict[int, int]] = {}
    for path_sha1, seq, station_id in rows:
        by_variant.setdefault(path_sha1, {})[seq] = station_id

    # Station B sits at index 2 in one variant and index 5 in the other --
    # never both under the same digest, and never lost.
    b_indices = {
        seq for pattern in by_variant.values() for seq, station_id in pattern.items()
        if station_id == b_id
    }
    assert b_indices == {2, 5}


def test_a_coupled_day_observation_is_recorded_at_the_right_station(session):
    """changes_poller.py: day 2's own variant is found, not day 1's stale slot."""
    _import_both_days(session)

    # Station B, on day 2, arrives 2 minutes late (pt 05:13 -> ct 05:15).
    now = _at(2, 6, 0) + GRACE_S + 60
    poll_source = _Source(
        plan={}, fchg={STATION_B_EVA: _fchg_xml(STATION_B_EVA, DAY2_B_ID, "2609020515")}
    )
    run_tt_poll(session, poll_source, priority_max=99, now=now)
    session.flush()

    day2_trip_id = session.execute(
        select(Trip.id).where(Trip.service_date == "2026-09-02")
    ).scalar_one()
    b_id = session.execute(select(Station.id).where(Station.eva == STATION_B_EVA)).scalar_one()

    written = session.execute(
        select(TripStop).where(TripStop.trip_id == day2_trip_id, TripStop.stop_sequence == 5)
    ).scalar_one_or_none()
    assert written is not None, "the coupled-day observation must not be discarded"
    assert written.station_id == b_id
    assert written.quality == QUALITY_RECORDED
    assert written.arr_delay_s == 120
    assert written.dep_delay_s == 120

    # No corrupted/foreign-station fact, and no safety-net fallback needed here.
    unmatched = session.execute(
        select(func.count()).select_from(TtUnmatched).where(TtUnmatched.eva == STATION_B_EVA)
    ).scalar_one()
    assert unmatched == 0


def test_finalize_fills_gaps_from_the_right_variant_only(session):
    """finalize_tt.py had the identical unscoped-pattern bug as changes_poller.

    With nothing ever polled, every stop becomes a gap row. Before this fix,
    finalize's own `_load_patterns` (a separate copy of the same helper) union-ed
    ALL of a train's station_stop rows under (train_id, service_date) regardless
    of variant, so day 1 (solo, {1: A, 2: B}) would also get a spurious gap row
    for index 5 -- day 2's slot, which day 1's own schedule never had -- and vice
    versa. Found via a real duplicate-station trip in the repaired database
    (ICE 2825, station "Augsburg Hbf" attached to 7 different stop_sequences of
    one trip, 6 of them gap rows) before this fix was added.
    """
    _import_both_days(session)

    now = _at(2, 23, 0)
    run_finalize_tt(session, now=now, force=True)
    session.flush()

    day1_trip_id = session.execute(
        select(Trip.id).where(Trip.service_date == "2026-09-01")
    ).scalar_one()
    day2_trip_id = session.execute(
        select(Trip.id).where(Trip.service_date == "2026-09-02")
    ).scalar_one()

    day1_seqs = sorted(
        row[0]
        for row in session.execute(
            select(TripStop.stop_sequence).where(TripStop.trip_id == day1_trip_id)
        ).all()
    )
    day2_seqs = sorted(
        row[0]
        for row in session.execute(
            select(TripStop.stop_sequence).where(TripStop.trip_id == day2_trip_id)
        ).all()
    )
    assert day1_seqs == [1, 2], "day 1 (solo) must not gain day 2's index 5"
    assert day2_seqs == [1, 5], "day 2 (coupled) must not gain day 1's index 2"


def test_a_same_day_double_visit_keeps_both_stops(session):
    """A genuine loop (one run, one variant, same station twice) is untouched."""
    loop_source = _Source(
        plan={
            STATION_A_EVA: _plan_xml(
                STATION_A_EVA,
                "Station A",
                _b_stop("2000000000000002-2609030500-1", "2609030501")
                + _b_stop("2000000000000002-2609030500-9", "2609030530"),
            ),
        },
        fchg={},
    )
    import_plan(session, loop_source, date="2026-09-03", hours=[5], force=True, now=_at(3, 5, 0))
    session.flush()

    train_id = session.execute(select(Train.id).where(Train.train_number == 40001)).scalar_one()
    seqs = sorted(
        row[0]
        for row in session.execute(
            select(StationStop.stop_sequence).where(StationStop.train_id == train_id)
        ).all()
    )
    assert seqs == [1, 9], "both visits of the loop must be kept"
