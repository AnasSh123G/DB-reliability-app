"""A deployed database's route-variant run summary answers exactly as the runs in `trip` do.

The same fixture is built twice: once with the runs in `trip` (the collection database), once
with only `train_variant_run` (the deployed database). Every variant choice must agree.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.deployed import TRAIN_VARIANT_RUN, view_metadata
from dbrel.identity import SOURCE_TT
from dbrel.ingest.runs import tt_feed_version
from dbrel.models import Station, StationStop, Train, Trip
from dbrel.web import queries

# train id -> {variant: station ids it calls at}
VARIANTS = {
    1: {"full": [3, 4], "short": [3]},  # the latest run was short, the full variant ran more often
    2: {"new": [3], "old": [3, 4]},  # "old" last ran 120 days before the newest run
    3: {None: [3], "x": [3, 4]},  # a variant without a digest (GTFS-style NULL) next to a named one
}
# train id -> [(service date, variant)]
RUNS = {
    1: [("2026-09-01", "full"), ("2026-09-02", "full"), ("2026-09-03", "full"), ("2026-09-10", "short")],
    2: [("2026-05-01", "old"), ("2026-05-02", "old"), ("2026-08-29", "new")],
    3: [("2026-09-01", None), ("2026-09-02", None), ("2026-08-01", "x")],
}


def _build(with_summary: bool) -> Session:
    """The fixture as a session: runs in `trip`, or only `train_variant_run` when `with_summary`."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine, future=True)
    fv_id = tt_feed_version(s)
    for sid in (3, 4):
        s.add(Station(id=sid, natural_key=f"s{sid}", name=f"Station {sid}", lat=51.0, lon=7.0 + sid, tracked=1))
    for train_id, variants in VARIANTS.items():
        s.add(Train(id=train_id, source=SOURCE_TT, identity_key=f"tts:RE/{train_id}", category="RE",
                    segment="RV", train_number=train_id, display_name=f"RE {train_id}", n_stops=2,
                    path_sha1=next(v for v in variants if v is not None)))
        s.flush()
        for variant, stations in variants.items():
            for seq, sid in enumerate(stations, start=1):
                s.add(StationStop(feed_version_id=fv_id, train_id=train_id, stop_sequence=seq, station_id=sid,
                                  planned_arrival_sec=8 * 3600 + 600 * seq,
                                  planned_departure_sec=8 * 3600 + 600 * seq + 60, path_sha1=variant))
        for date, variant in RUNS[train_id]:
            s.add(Trip(train_id=train_id, service_date=date, source_run_key=f"{train_id}-{date}", path_sha1=variant))
    s.flush()
    if with_summary:
        rows = s.execute(select(Trip.train_id, Trip.path_sha1, func.max(Trip.service_date), func.count())
                         .group_by(Trip.train_id, Trip.path_sha1)).all()
        s.query(Trip).delete()
        view_metadata.create_all(s.connection())  # here a plain table; a view in a real build
        s.execute(insert(TRAIN_VARIANT_RUN), [
            {"train_id": t, "path_sha1": v, "last_service_date": last, "n_runs": n} for t, v, last, n in rows])
        s.flush()
    return s


@pytest.fixture(params=[False, True], ids=["trip", "summary"])
def session(request):
    """A fixture session, once with runs and once with only the summary."""
    s = _build(request.param)
    yield s
    s.close()


def _stops(route):
    """The station names of a train route response, in order."""
    return [stop["name"] for stop in route["stops"]]


def test_the_summary_is_what_this_session_reads(session):
    """Each fixture reads its own source, and both give the same numbers."""
    has_trip_rows = session.execute(select(func.count()).select_from(Trip)).scalar_one() > 0
    assert queries._variant_runs(session, [1])[(1, "short")] == ("2026-09-10", 1)
    assert queries._variant_runs(session, [1])[(1, "full")] == ("2026-09-03", 3)
    assert has_trip_rows != queries._run_summary_present[session.get_bind()]


def test_the_latest_run_decides_the_route_without_a_stop(session):
    """Without a stop, the most recent run's variant is shown."""
    assert _stops(queries.train_route(session, 1)) == ["Station 3"]  # short, though full ran 3 times
    assert _stops(queries.train_route(session, 2)) == ["Station 3"]  # new, not the old variant
    assert _stops(queries.train_route(session, 3)) == ["Station 3", "Station 4"]  # x: NULL is never "latest"


def test_the_ranking_at_a_station_is_the_same_on_both_databases(session):
    """The variant ranking at a station follows (last run, run count)."""
    fv_id = tt_feed_version(session)
    chosen = queries._variant_per_train_at(session, 3, fv_id, [1, 2, 3])
    # (last run, run count) decides: short ran last; new ran last; the NULL variant ran last.
    assert chosen == {1: "short", 2: "new", 3: None}


def test_both_databases_agree_on_every_choice():
    """Runs and summary pick the same variant for every train and stop."""
    collection, deployed = _build(False), _build(True)
    fv = tt_feed_version(collection)
    for station_id in (3, 4):
        assert (queries._variant_per_train_at(collection, station_id, fv, [1, 2, 3])
                == queries._variant_per_train_at(deployed, station_id, tt_feed_version(deployed), [1, 2, 3]))
    for train_id in VARIANTS:
        for station_id in (None, 3, 4):
            assert (_stops(queries.train_route(collection, train_id, station_id=station_id))
                    == _stops(queries.train_route(deployed, train_id, station_id=station_id)))
