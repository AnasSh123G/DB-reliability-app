import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import FeedVersion, Station, StationStop, Train, Trip
from dbrel.ingest.parser import TtEvent, TtStop
from dbrel.ingest.dated_schedule import PlanReceipt, record_plans, load_patterns


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        feed = FeedVersion(kind="rv", source_url="x", content_sha256="a" * 64, downloaded_at=1)
        station = Station(natural_key="x", name="X", lat=0, lon=0)
        train = Train(source=SOURCE_TT, identity_kind="NUMBERED", identity_key="tts:RE/1", train_number=1, route_path="1|2", path_sha1="path-a")
        s.add_all([feed, station, train]); s.flush()
        trip = Trip(train_id=train.id, service_date="2026-01-02", source=SOURCE_TT, source_run_key="run", path_sha1="path-a", feed_version_id=feed.id)
        s.add(trip); s.flush()
        yield s, feed, station, train, trip


def stop(eva=100, seq=1, arr=1000, dep=1100, sid="s1", stamp="260102000000"):
    def event(ts):
        return TtEvent(planned_ts=ts, changed_ts=None, planned_platform="3", changed_platform=None,
            planned_status=None, changed_status=None, planned_path=None, changed_path=None,
            line="RE 1", cancelled_at=None, hidden=False, wings=None, transition=None)
    return TtStop(sid, eva, "run", stamp, seq, "RE", "1", None, None, None, None,
                  event(arr), event(dep))


def receipt(at=50, sha="a" * 64):
    return PlanReceipt(at, sha, eva=100, plan_date="2026-01-02", plan_hour=0)


def test_receipt_idempotency_and_as_of(session):
    s, _, station, train, trip = session
    next_day = Trip(train_id=train.id, service_date="2026-01-03", source=SOURCE_TT, source_run_key="run-next", path_sha1="path-b")
    s.add(next_day); s.flush()
    item = stop()
    assert record_plans(s, trip.id, "path-a", [(item, receipt())], {100: station.id}) == 1
    assert record_plans(s, trip.id, "path-a", [(item, receipt())], {100: station.id}) == 0
    assert record_plans(s, next_day.id, "path-b", [(
        stop(stamp="260103000000"),
        PlanReceipt(70, "b" * 64, eva=100, plan_date="2026-01-03", plan_hour=0),
    )], {100: station.id}) == 1
    dates = {train.id: {"2026-01-02": "path-a", "2026-01-03": "path-b"}}
    assert load_patterns(s, [train.id], dates, as_of=49)[(train.id, "2026-01-02")] == {}
    assert load_patterns(s, [train.id], dates, as_of=50)[(train.id, "2026-01-02")][1].supposed_arr == 1000
    dated = load_patterns(s, [train.id], dates)
    assert dated[(train.id, "2026-01-03")][1].supposed_arr == 1000
    assert dated[(train.id, "2026-01-02")][1].supposed_arr == 1000
    assert dated[(train.id, "2026-01-03")][1].supposed_arr == 1000


def test_dated_snapshots_do_not_use_shared_pattern_for_tt(session):
    s, feed, station, train, trip = session
    s.add(StationStop(feed_version_id=feed.id, train_id=train.id, path_sha1="path-a", stop_sequence=1, station_id=station.id, planned_arrival_sec=3600))
    s.flush()
    assert load_patterns(s, [train.id], {train.id: {trip.service_date: "path-a"}})[(train.id, trip.service_date)] == {}


def test_same_date_conflicting_values_are_unknown_per_side(session):
    s, _, station, train, trip = session
    first = stop(arr=1000, dep=1100)
    second = stop(arr=1200, dep=1100, sid="s2")
    assert record_plans(s, trip.id, "path-a", [(first, receipt(50)), (second, receipt(60, "b" * 64))], {100: station.id}) == 2
    planned = load_patterns(s, [train.id], {train.id: {trip.service_date: "path-a"}})[(train.id, trip.service_date)][1]
    assert planned.supposed_arr is None
    assert planned.supposed_dep == 1100


def test_wrong_path_or_station_snapshot_is_ignored(session):
    from dbrel.models import TripScheduleSnapshot

    s, _, station, train, trip = session
    other_station = Station(natural_key="y", name="Y", lat=1, lon=1)
    s.add(other_station); s.flush()
    s.add(TripScheduleSnapshot(
        trip_id=trip.id, stop_sequence=1, station_id=station.id, path_sha1="other",
        supposed_arrival_utc=1000, supposed_departure_utc=1100, observed_at=10,
        source_kind="plan", source_content_sha256="a" * 64, source_stop_id="s1",
        value_fingerprint="c" * 64,
    ))
    s.add(TripScheduleSnapshot(
        trip_id=trip.id, stop_sequence=1, station_id=station.id, path_sha1="path-a",
        supposed_arrival_utc=1000, supposed_departure_utc=1100, observed_at=11,
        source_kind="plan", source_content_sha256="b" * 64, source_stop_id="s1",
        value_fingerprint="d" * 64,
    ))
    s.add(TripScheduleSnapshot(
        trip_id=trip.id, stop_sequence=1, station_id=other_station.id, path_sha1="path-a",
        supposed_arrival_utc=1000, supposed_departure_utc=1100, observed_at=12,
        source_kind="plan", source_content_sha256="d" * 64, source_stop_id="s2",
        value_fingerprint="e" * 64,
    ))
    s.flush()
    assert load_patterns(s, [train.id], {train.id: {trip.service_date: "path-a"}})[(train.id, trip.service_date)] == {}


