"""The observation instant is a parameter, not the wall clock.

Two things are pinned here, and both are load-bearing for the archive backfill
(openspec/changes/add-archive-backfill, design.md decision 2).

First, that threading `now` through changed nothing for live callers: omitting it
must grade exactly as passing the clock's own value.

Second, that grading follows the instant supplied rather than the instant the code
runs. IRIS's `ct` is a forecast before a train passes and DB's recorded time after
it, and nothing in the payload says which — only the clock does. Replaying an
archived response months later must therefore judge it against the instant it was
*recorded*, or every forecast in the archive would be credited as a measurement.
That is precisely the defect that made the upstream project's processed data
unusable, and it is the one this parameter exists to avoid.

The fixtures are the real Dormagen stop, copied verbatim from the archive
(raw_data/year=2026/month=8/day=28) rather than invented: NX 26801 on RE1, booked
03:59/04:00 on 2026-08-28, which IRIS reported as running exactly to time.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dbrel.config import GRACE_S, TZ
from dbrel.db import Base
from dbrel.ingest.changes_poller import run_tt_poll
from dbrel.ingest.client import Response
from dbrel.ingest.plan_import import import_plan
from dbrel.models import QUALITY_RECORDED, Station, TripStop, TtPoll

EVA = 8001506
STOP_ID = "-5342035254732082363-2608280320-4"
STOP_INDEX = 4

# Verbatim from the archive's /plan response for eva 08001506, hour 03.
PLAN_XML = (
    '<timetable station="Dormagen" eva="8001506">'
    f'<s id="{STOP_ID}">'
    '<tl t="p" o="NXRE" c="NX" n="26801"/>'
    '<ar pt="2608280359" pp="1" l="RE1" fb="RE1" ppth="Köln Messe/Deutz|Köln Hbf"/>'
    '<dp pt="2608280400" pp="1" l="RE1" fb="RE1" ppth="Neuss Hbf|Düsseldorf Hbf"/>'
    "</s></timetable>"
).encode()

# Verbatim from the archive's /fchg response, polled 2026-08-28 00:57:20 -- which is
# three hours BEFORE the stop it describes. Under the real clock that made it a
# forecast; the 15:20 sweep later re-published the same values as measurements.
FCHG_XML = (
    '<timetable station="Dormagen" eva="8001506">'
    f'<s id="{STOP_ID}" eva="8001506">'
    '<ar ct="2608280359" l="RE1"/>'
    '<dp ct="2608280400" l="RE1"/>'
    "</s></timetable>"
).encode()


def _at(hour: int, minute: int = 0) -> int:
    """Epoch seconds of a local wall-clock time on the fixture's service date."""
    return int(datetime(2026, 8, 28, hour, minute, tzinfo=TZ).timestamp())


DEPARTURE = _at(4, 0)
BEFORE = DEPARTURE - 3 * 3600  # the real 00:57 poll: a forecast
AFTER = DEPARTURE + GRACE_S + 60  # a later sweep: a measurement


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Station(
                natural_key="dormagen@51.099,6.837",
                name="Dormagen",
                lat=51.099,
                lon=6.837,
                eva=EVA,
                tracked=1,
            )
        )
        session.commit()
        yield session


class _FakeClient:
    """Serves the fixtures the way the replay source will serve the archive."""

    def plan(self, eva: int, date: str, hour: int) -> Response:
        return Response(status=200, body=PLAN_XML, elapsed_ms=1)

    def changes(self, eva: int) -> Response:
        return Response(status=200, body=FCHG_XML, elapsed_ms=1)


def _seed_schedule(session: Session, now: int | None = None) -> None:
    """The planned side must exist before /fchg has anything to attach to."""
    import_plan(session, _FakeClient(), date="2026-08-28", hours=[3], now=now)
    session.flush()


def _stop(session: Session) -> TripStop | None:
    return session.execute(
        select(TripStop).where(TripStop.stop_sequence == STOP_INDEX)
    ).scalar_one_or_none()


# --- task 1.3: live behaviour is unchanged ---------------------------------


def test_omitting_now_matches_passing_the_clocks_own_value(session, monkeypatch):
    """A live caller and an explicit caller at the same instant must agree.

    Not a tautology: `now` is read in four places (the sweep id, the tt_poll
    listening record, observed_at behind read_lag_s, and the _has_happened
    filter). A miss in any one of them would show up as a differing column here.
    """
    _seed_schedule(session, now=AFTER)
    run_tt_poll(session, _FakeClient(), now=AFTER)
    session.flush()
    explicit = _stop(session)
    assert explicit is not None
    fields = {
        c.name: getattr(explicit, c.name)
        for c in TripStop.__table__.columns
        if c.name != "id"
    }
    explicit_sweeps = session.execute(select(TtPoll.sweep_id, TtPoll.fetched_at)).all()

    # Same fixtures, same instant -- but reached through the wall clock.
    other = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(other)
    with Session(other) as live:
        live.add(
            Station(
                natural_key="dormagen@51.099,6.837",
                name="Dormagen",
                lat=51.099,
                lon=6.837,
                eva=EVA,
                tracked=1,
            )
        )
        live.commit()
        monkeypatch.setattr("time.time", lambda: float(AFTER))
        import_plan(live, _FakeClient(), date="2026-08-28", hours=[3])
        live.flush()
        run_tt_poll(live, _FakeClient())
        live.flush()
        implicit = live.execute(
            select(TripStop).where(TripStop.stop_sequence == STOP_INDEX)
        ).scalar_one()
        live_fields = {
            c.name: getattr(implicit, c.name)
            for c in TripStop.__table__.columns
            if c.name != "id"
        }
        live_sweeps = live.execute(select(TtPoll.sweep_id, TtPoll.fetched_at)).all()

    assert live_fields == fields, "threading `now` must not change live grading"
    assert live_sweeps == explicit_sweeps, "the listening record must match too"


# --- task 1.4: grading follows the supplied instant -----------------------


def test_a_value_published_before_its_event_is_not_a_measurement(session):
    """The 00:57 forecast must not be credited as an observed actual.

    The poller does not grade this stop at all -- it counts it pending and leaves
    it for `finalize`, which owns the stops that produced no value. What matters
    for the spec is the negative: no RECORDED row appears.
    """
    _seed_schedule(session, now=BEFORE)
    result = run_tt_poll(session, _FakeClient(), now=BEFORE)
    session.flush()

    assert result.pending == 1, "a future event is pending, not recorded"
    assert result.stored == 0
    assert _stop(session) is None, "a forecast must not enter the fact table"


def test_a_later_sweep_supersedes_an_earlier_forecast(session):
    """Replay order matters: the forecast sweep runs first, as it did live."""
    _seed_schedule(session, now=BEFORE)
    run_tt_poll(session, _FakeClient(), now=BEFORE)
    session.flush()
    assert _stop(session) is None

    run_tt_poll(session, _FakeClient(), now=AFTER)
    session.flush()
    stop = _stop(session)
    assert stop is not None
    assert stop.quality == QUALITY_RECORDED
    assert stop.actual_departure_utc == DEPARTURE


def test_grading_does_not_depend_on_when_the_replay_runs(session, monkeypatch):
    """The same archive window replayed at any later date must grade the same."""
    _seed_schedule(session, now=AFTER)
    monkeypatch.setattr("time.time", lambda: float(AFTER + 400 * 86_400))
    run_tt_poll(session, _FakeClient(), now=AFTER)
    session.flush()

    stop = _stop(session)
    assert stop is not None
    assert stop.last_observed_at == AFTER, "wall clock must not leak into the record"
    assert stop.read_lag_s == AFTER - DEPARTURE
