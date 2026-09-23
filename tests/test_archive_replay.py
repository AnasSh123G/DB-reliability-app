"""The guarantees that make a backfill from raw responses worth doing.

The whole reason for replaying `raw_data` rather than importing the mirror's
`monthly_processed_data` is that the processed table asserts on-time running for
stops nobody ever measured. Its merge falls back
`COALESCE(f.departure_change_time, p.departure_planned_time, ...)`, so a stop with
no `/fchg` report becomes `delay_in_min = 0`, indistinguishable from a train that
genuinely ran to the minute.

Both cases are built here from stops verified against the real archive, and the
test that matters is that they come out *different*:

  Dormagen, NX 26801 on RE1  -- `/fchg` published `ct` equal to `pt`. A real zero.
  Lennestadt-Meggen, Bus 8228 -- appears in no `/fchg` response all day. No zero
                                 at all, and certainly not a punctual one.

(Bus 8228 is a replacement bus, and NON_RAIL_CATEGORIES filters `Bus` before it
reaches the fact table -- which is itself part of why the mirror's 55.6% fake-zero
rate for buses never touches this project. The never-reported case is therefore
also exercised with a rail stop, so the guarantee is not resting on that filter.)
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
from dbrel.ingest.plan_import import import_plan
from dbrel.ingest.replay import ArchiveReplaySource, ReplayCounters
from dbrel.models import QUALITY_RECORDED, Station, TripStop

# --- the two verified stops ------------------------------------------------

PUNCTUAL_EVA = 8001506  # Dormagen
PUNCTUAL_ID = "-5342035254732082363-2608280320-4"
PUNCTUAL_INDEX = 4

SILENT_EVA = 8000823  # a rail stop on the same fixture day
SILENT_ID = "-9147199812937242171-2608280348-3"
SILENT_INDEX = 3


def _plan_xml(eva: int, station: str, stops: str) -> bytes:
    return f'<timetable station="{station}" eva="{eva}">{stops}</timetable>'.encode()


# Both stops are booked; this is the schedule, and it is all /plan ever carries.
PUNCTUAL_PLAN_STOP = (
    f'<s id="{PUNCTUAL_ID}">'
    '<tl t="p" o="NXRE" c="NX" n="26801"/>'
    '<ar pt="2608280359" pp="1" l="RE1" fb="RE1" ppth="Köln Hbf"/>'
    '<dp pt="2608280400" pp="1" l="RE1" fb="RE1" ppth="Neuss Hbf"/>'
    "</s>"
)
SILENT_PLAN_STOP = (
    f'<s id="{SILENT_ID}">'
    '<tl t="p" o="R4NRN" c="RB" n="8228"/>'
    '<ar pt="2608280403" pp="1" l="RB91" fb="RB91" ppth="Finnentrop"/>'
    '<dp pt="2608280403" pp="1" l="RB91" fb="RB91" ppth="Kirchhundem"/>'
    "</s>"
)

# Verbatim from the archive: IRIS published this train as running exactly to time.
PUNCTUAL_FCHG = _plan_xml(
    PUNCTUAL_EVA,
    "Dormagen",
    f'<s id="{PUNCTUAL_ID}" eva="{PUNCTUAL_EVA}">'
    '<ar ct="2608280359" l="RE1"/><dp ct="2608280400" l="RE1"/></s>',
)
# And for the silent stop, /fchg says nothing at all. Not an empty ct -- absent.
SILENT_FCHG = _plan_xml(SILENT_EVA, "Lennestadt-Meggen", "")


def _at(hour: int, minute: int) -> int:
    return int(datetime(2026, 8, 28, hour, minute, tzinfo=TZ).timestamp())


PUNCTUAL_DEP = _at(4, 0)
SILENT_DEP = _at(4, 3)
AFTER = SILENT_DEP + GRACE_S + 60
BEFORE = _at(0, 57)  # the real forecast sweep


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Station(
                    natural_key="dormagen@51.099,6.837",
                    name="Dormagen",
                    lat=51.099,
                    lon=6.837,
                    eva=PUNCTUAL_EVA,
                    tracked=1,
                ),
                Station(
                    natural_key="lennestadt-meggen@51.116,8.053",
                    name="Lennestadt-Meggen",
                    lat=51.116,
                    lon=8.053,
                    eva=SILENT_EVA,
                    tracked=1,
                ),
            ]
        )
        session.commit()
        yield session


class _Source:
    """Both stations' fixtures, in the shape the replay source presents."""

    def __init__(self, *, fchg: dict[int, bytes] | None = None) -> None:
        self._plan = {
            PUNCTUAL_EVA: _plan_xml(PUNCTUAL_EVA, "Dormagen", PUNCTUAL_PLAN_STOP),
            SILENT_EVA: _plan_xml(SILENT_EVA, "Lennestadt-Meggen", SILENT_PLAN_STOP),
        }
        self._fchg = fchg if fchg is not None else {
            PUNCTUAL_EVA: PUNCTUAL_FCHG,
            SILENT_EVA: SILENT_FCHG,
        }

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


def _replay(session: Session, *, now: int, source: _Source | None = None):
    source = source or _Source()
    import_plan(session, source, date="2026-08-28", hours=[3, 4], force=True, now=now)
    session.flush()
    result = run_tt_poll(session, source, priority_max=99, now=now)
    session.flush()
    return result


def _stop(session: Session, index: int) -> TripStop | None:
    return session.execute(
        select(TripStop).where(TripStop.stop_sequence == index)
    ).scalar_one_or_none()


# --- task 5.2: a genuine zero stays a zero --------------------------------


def test_a_train_reported_exactly_to_time_is_a_recorded_zero(session):
    _replay(session, now=AFTER)
    stop = _stop(session, PUNCTUAL_INDEX)
    assert stop is not None, "IRIS published a ct for this stop"
    assert stop.quality == QUALITY_RECORDED
    assert stop.actual_departure_utc == PUNCTUAL_DEP
    assert stop.dep_delay_s == 0
    assert stop.arr_delay_s == 0


# --- task 5.1: a never-reported stop is never a zero ----------------------


def test_a_stop_no_fchg_ever_mentioned_gets_no_actual_and_no_delay(session):
    """The poller alone cannot fabricate one, because it never sees the stop.

    Worth stating explicitly rather than assuming: run_tt_poll iterates /fchg's
    stops, so a stop absent from /fchg is invisible to it and no row can be
    invented here. The guarantee that a *scheduled* stop is not written up as
    punctual is therefore not testable at this level -- see the finalize tests
    at the end of this file, which is where the plan side is walked.
    """
    _replay(session, now=AFTER)
    stop = _stop(session, SILENT_INDEX)

    if stop is not None:
        # If a row exists at all it must carry nothing, and must not be RECORDED.
        assert stop.actual_arrival_utc is None
        assert stop.actual_departure_utc is None
        assert stop.arr_delay_s is None
        assert stop.dep_delay_s is None
        assert stop.quality != QUALITY_RECORDED
    # Either way, the assertion that matters: no zero-delay row was invented.
    invented = session.scalar(
        select(func.count())
        .select_from(TripStop)
        .where(TripStop.stop_sequence == SILENT_INDEX, TripStop.dep_delay_s == 0)
    )
    assert invented == 0, "a stop with no report must not be published as on time"


def test_the_planned_time_is_never_copied_into_the_actual(session):
    """Precisely the COALESCE the mirror's processed parquet performs."""
    _replay(session, now=AFTER)
    rows = session.execute(select(TripStop)).scalars().all()
    for row in rows:
        if row.quality == QUALITY_RECORDED:
            continue
        assert row.actual_departure_utc != row.supposed_departure_utc or (
            row.actual_departure_utc is None
        ), "an unobserved stop must not be handed its own schedule as an actual"


# --- task 5.3: and the two stay distinguishable ---------------------------


def test_the_measured_and_the_unmeasured_do_not_look_alike(session):
    """In the mirror's processed table these two rows are identical. Here they are not."""
    _replay(session, now=AFTER)
    punctual = _stop(session, PUNCTUAL_INDEX)
    silent = _stop(session, SILENT_INDEX)

    assert punctual is not None
    assert punctual.quality == QUALITY_RECORDED
    if silent is not None:
        assert silent.quality != punctual.quality

    recorded = session.execute(
        select(TripStop.stop_sequence).where(TripStop.quality == QUALITY_RECORDED)
    ).scalars().all()
    assert PUNCTUAL_INDEX in recorded
    assert SILENT_INDEX not in recorded, "filtering on RECORDED must exclude it"


def test_averaging_the_recorded_rows_sees_only_real_measurements(session):
    """The query the web board and the aggregates actually run."""
    _replay(session, now=AFTER)
    mean = session.scalar(
        select(func.avg(TripStop.dep_delay_s)).where(
            TripStop.quality == QUALITY_RECORDED, TripStop.dep_delay_s.isnot(None)
        )
    )
    count = session.scalar(
        select(func.count()).select_from(TripStop).where(TripStop.quality == QUALITY_RECORDED)
    )
    assert count == 1, "one stop was measured, one was not"
    assert mean == 0, "and the measured one really was on time"


# --- task 5.4: forecasts ---------------------------------------------------


def test_a_forecast_sweep_records_nothing_then_a_later_sweep_does(session):
    result = _replay(session, now=BEFORE)
    assert _stop(session, PUNCTUAL_INDEX) is None
    assert result.pending >= 1

    _replay(session, now=AFTER)
    stop = _stop(session, PUNCTUAL_INDEX)
    assert stop is not None
    assert stop.quality == QUALITY_RECORDED
    assert stop.actual_departure_utc == PUNCTUAL_DEP


# --- task 5.5: idempotence and non-downgrade ------------------------------


def _snapshot(session: Session) -> list[tuple]:
    return [
        tuple(getattr(row, c.name) for c in TripStop.__table__.columns if c.name != "id")
        for row in session.execute(
            select(TripStop).order_by(TripStop.trip_id, TripStop.stop_sequence)
        ).scalars()
    ]


def test_replaying_the_same_window_twice_changes_nothing(session):
    _replay(session, now=AFTER)
    first = _snapshot(session)
    count_first = session.scalar(select(func.count()).select_from(TripStop))

    second_result = _replay(session, now=AFTER)
    assert _snapshot(session) == first
    assert session.scalar(select(func.count()).select_from(TripStop)) == count_first
    assert second_result.stored == 0, "nothing was rewritten with its own contents"


def test_a_recorded_stop_is_not_downgraded_by_a_later_silent_sweep(session):
    """A sweep that missed the station must not undo what an earlier one measured."""
    _replay(session, now=AFTER)
    before = _stop(session, PUNCTUAL_INDEX)
    assert before is not None and before.quality == QUALITY_RECORDED
    measured = before.actual_departure_utc

    # A later sweep in which this station returned nothing at all.
    silent_source = _Source(fchg={})
    run_tt_poll(session, silent_source, priority_max=99, now=AFTER + 3600)
    session.flush()

    after = _stop(session, PUNCTUAL_INDEX)
    assert after is not None
    assert after.quality == QUALITY_RECORDED
    assert after.actual_departure_utc == measured


def test_no_duplicate_rows_across_repeated_replays(session):
    for _ in range(3):
        _replay(session, now=AFTER)
    duplicates = session.execute(
        select(TripStop.trip_id, TripStop.stop_sequence, func.count())
        .group_by(TripStop.trip_id, TripStop.stop_sequence)
        .having(func.count() > 1)
    ).all()
    assert duplicates == []


# --- task 5.7: station scoping --------------------------------------------


def test_an_untracked_eva_is_counted_and_written_nowhere():
    counters = ReplayCounters()
    from dbrel.ingest.archive import API_FCHG, ArchivedResponse

    responses = [
        ArchivedResponse(recorded_at=AFTER, api_name=API_FCHG, eva=PUNCTUAL_EVA, body=PUNCTUAL_FCHG),
        ArchivedResponse(recorded_at=AFTER, api_name=API_FCHG, eva=9999999, body=PUNCTUAL_FCHG),
    ]
    source = ArchiveReplaySource(responses, tracked_evas={PUNCTUAL_EVA}, counters=counters)

    assert source.changes(PUNCTUAL_EVA).status == 200
    assert source.changes(9999999).status == 404, "not served, because not tracked"
    assert counters.untracked_evas == {9999999}


def test_a_tracked_station_the_archive_never_covered_is_reported_not_fatal(session):
    """Only one of the two stations appears in the sweep."""
    source = _Source(fchg={PUNCTUAL_EVA: PUNCTUAL_FCHG})
    import_plan(session, source, date="2026-08-28", hours=[3, 4], force=True, now=AFTER)
    session.flush()
    result = run_tt_poll(session, source, priority_max=99, now=AFTER)

    assert result.stations_polled + result.stations_failed >= 2
    assert result.stations_failed >= 1, "the uncovered station is recorded as a failed read"
    assert _stop(session, PUNCTUAL_INDEX) is not None, "the covered one still landed"


# --- task 5.1 / 5.6, properly: the guarantee lives in finalize ------------
#
# The poller only ever walks /fchg, so a scheduled stop the archive never
# reported is structurally invisible to it -- which means a test that only runs
# the poller cannot detect a fabricated zero at all. The mirror's COALESCE bug
# has its analogue on the PLAN side, and in this project the plan side is walked
# by finalize. That is where these assertions belong.


def _finalize(session: Session, now: int):
    from dbrel.ingest.finalize_tt import run_finalize_tt

    result = run_finalize_tt(session, now=now, force=True)
    session.flush()
    return result


def test_finalize_grades_the_never_reported_stop_instead_of_inventing_a_zero(session):
    """The whole point: absent data becomes a stated absence, not punctuality."""
    from dbrel.models import QUALITY_NO_DATA, QUALITY_PREDICTION_ONLY

    _replay(session, now=AFTER)
    _finalize(session, now=AFTER + 7200)

    stop = _stop(session, SILENT_INDEX)
    assert stop is not None, "finalize must account for the scheduled stop, not drop it"
    assert stop.quality in (QUALITY_NO_DATA, QUALITY_PREDICTION_ONLY)
    assert stop.actual_arrival_utc is None
    assert stop.actual_departure_utc is None
    assert stop.arr_delay_s is None, "no delay can be stated without a measurement"
    assert stop.dep_delay_s is None
    assert stop.supposed_departure_utc == SILENT_DEP, "the schedule is still known"


def test_no_fabricated_zero_survives_finalize(session):
    """A zero delay may exist only where a ct was actually published."""
    _replay(session, now=AFTER)
    _finalize(session, now=AFTER + 7200)

    zeros = session.execute(
        select(TripStop).where(TripStop.dep_delay_s == 0)
    ).scalars().all()
    assert len(zeros) == 1, "exactly one train was reported to the minute"
    assert zeros[0].stop_sequence == PUNCTUAL_INDEX
    assert zeros[0].quality == QUALITY_RECORDED


def test_the_measured_and_unmeasured_stay_distinguishable_after_finalize(session):
    """In the mirror's processed table these two rows are byte-identical."""
    _replay(session, now=AFTER)
    _finalize(session, now=AFTER + 7200)

    punctual = _stop(session, PUNCTUAL_INDEX)
    silent = _stop(session, SILENT_INDEX)
    assert punctual is not None and silent is not None
    assert punctual.quality == QUALITY_RECORDED
    assert silent.quality != QUALITY_RECORDED

    # Both rows exist, so a naive average over all of them would be wrong -- and
    # filtering on the grade is what makes it right.
    naive = session.scalar(select(func.avg(TripStop.dep_delay_s)))
    trusted = session.scalar(
        select(func.avg(TripStop.dep_delay_s)).where(TripStop.quality == QUALITY_RECORDED)
    )
    assert trusted == 0
    assert naive == 0, "the unmeasured row contributes no number at all, not a zero"
    assert session.scalar(
        select(func.count()).select_from(TripStop).where(TripStop.dep_delay_s.is_(None))
    ) == 1, "the unmeasured stop's delay is NULL, so SQL averages exclude it"


def test_an_uncovered_window_records_a_measured_gap(session):
    """No poll covered this stop at all: PREDICTION_ONLY with poll_gap_s."""
    from dbrel.models import QUALITY_PREDICTION_ONLY

    # Plan only -- no sweep whatsoever, which is what an archive hole looks like.
    source = _Source(fchg={})
    import_plan(session, source, date="2026-08-28", hours=[3, 4], force=True, now=AFTER)
    session.flush()
    _finalize(session, now=AFTER + 7200)

    graded = session.execute(select(TripStop)).scalars().all()
    assert graded, "an uncovered window must still be accounted for"
    for row in graded:
        assert row.quality == QUALITY_PREDICTION_ONLY
        assert row.dep_delay_s is None and row.arr_delay_s is None
        assert row.poll_gap_s is not None, "the blind spot's width must be recorded"
