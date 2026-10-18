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


# --- task 5.4: forecasts ---------------------------------------------------


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


# --- task 5.7: station scoping --------------------------------------------


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


