"""_average's provisional marking and resolve_basis's REAL-first fallback.

Both changed together: with no synthetic data seeded (dbrel seed-demo never
run), resolve_basis used to fall back to BASIS_SYNTH purely because no REAL
row had n_dep >= AVG_MIN_SAMPLES -- without ever checking a SYNTH row
existed. That put the "Simulated data... every figure below is generated"
banner over a board with nothing generated and nothing shown. _average used
to hide any average below the same threshold, which is what made that
fallback seem necessary in the first place; showing a figure from the first
sample (marked provisional) removes the need for it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.config import AVG_MIN_SAMPLES
from dbrel.db import Base
from dbrel.models import BASIS_REAL, BASIS_SYNTH, Station, Train, TrainStationDelay
from dbrel.web.queries import _average, resolve_basis


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Station(id=1, natural_key="x@1,1", name="X Hbf", lat=1, lon=1))
        s.add(Train(id=1, source="tts", identity_key="tts:RE/1", category="RE", segment="RV"))
        s.flush()
        yield s


def _row(**overrides):
    defaults = dict(
        station_id=1, train_id=1, basis=BASIS_REAL, dow=0,
        n_arr=1, n_dep=1, n_runs=1, n_known=1, n_cancelled=0,
        avg_dep_delay_s=60.0, avg_arr_delay_s=60.0,
        p50_dep_delay_s=60, p90_dep_delay_s=60,
        p50_arr_delay_s=60, p90_arr_delay_s=60,
        pct_dep_punctual=1.0, pct_arr_punctual=1.0,
        computed_at=1, rule_version=1,
    )
    defaults.update(overrides)
    return TrainStationDelay(**defaults)


# --------------------------------------------------------------------------
# _average
# --------------------------------------------------------------------------


def test_average_shows_a_figure_from_a_single_sample():
    row = _row(n_dep=1)
    avg = _average(row, "dep")
    assert avg is not None
    assert avg["n"] == 1
    assert avg["provisional"] is True


def test_average_is_not_provisional_at_the_sample_floor():
    row = _row(n_dep=AVG_MIN_SAMPLES)
    avg = _average(row, "dep")
    assert avg["provisional"] is False


def test_average_is_none_only_with_zero_samples():
    row = _row(n_dep=0, avg_dep_delay_s=None)
    assert _average(row, "dep") is None


def test_average_is_none_for_a_missing_row():
    assert _average(None, "dep") is None


# --------------------------------------------------------------------------
# resolve_basis
# --------------------------------------------------------------------------


def test_resolve_basis_prefers_real_with_no_synth_rows_at_all(session):
    """The bug: a fresh, no-synthetic-data database with one thin real row
    must not fall back to SYNTH just because it is below AVG_MIN_SAMPLES."""
    session.add(_row(basis=BASIS_REAL, n_dep=1))
    session.flush()
    assert resolve_basis(session) == BASIS_REAL


def test_resolve_basis_is_real_on_a_completely_empty_table(session):
    assert resolve_basis(session) == BASIS_REAL


def test_resolve_basis_falls_back_to_synth_when_synth_rows_exist_and_no_real_rows_do(session):
    session.add(_row(basis=BASIS_SYNTH, n_dep=20))
    session.flush()
    assert resolve_basis(session) == BASIS_SYNTH


def test_resolve_basis_prefers_real_over_synth_when_both_exist(session):
    session.add(_row(basis=BASIS_REAL, n_dep=1))
    session.add(_row(basis=BASIS_SYNTH, n_dep=20, n_arr=20, n_runs=20, n_known=20))
    session.flush()
    assert resolve_basis(session) == BASIS_REAL


def test_resolve_basis_honours_an_explicit_request_either_way(session):
    session.add(_row(basis=BASIS_REAL, n_dep=20))
    session.flush()
    assert resolve_basis(session, "synth") == BASIS_SYNTH
    assert resolve_basis(session, "real") == BASIS_REAL
    assert resolve_basis(session, "garbage") == BASIS_REAL
