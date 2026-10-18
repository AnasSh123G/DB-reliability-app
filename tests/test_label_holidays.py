"""trip_stop.is_holiday labelling: the per-station, per-date holiday rule."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.derive.label_holidays import label_trip_stop_holidays
from dbrel.holidays_de import ALL_STATE_CODES
from dbrel.models import PublicHoliday, Station, Train, Trip, TripStop


@pytest.fixture
def engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Station(id=1, natural_key="a", name="NW station", lat=1, lon=1, state="DE-NW"))
        s.add(Station(id=2, natural_key="b", name="HH station", lat=2, lon=2, state="DE-HH"))
        s.add(Station(id=3, natural_key="c", name="Unknown station", lat=3, lon=3, state=None))
        s.add(Train(id=1, identity_key="t1"))
        s.add(Trip(id=1, train_id=1, service_date="2026-06-04", source_run_key="r1"))  # Fronleichnam
        s.add(Trip(id=2, train_id=1, service_date="2026-09-20", source_run_key="r2"))  # Sunday
        s.add(Trip(id=3, train_id=1, service_date="2026-10-03", source_run_key="r3"))  # nationwide
        s.add(Trip(id=4, train_id=1, service_date="2026-09-15", source_run_key="r4"))  # plain Tue
        s.add(TripStop(id=1, trip_id=1, stop_sequence=1, station_id=1, quality="RECORDED", rule_version=1))
        s.add(TripStop(id=2, trip_id=1, stop_sequence=2, station_id=2, quality="RECORDED", rule_version=1))
        s.add(TripStop(id=3, trip_id=1, stop_sequence=3, station_id=3, quality="RECORDED", rule_version=1))
        s.add(TripStop(id=4, trip_id=2, stop_sequence=1, station_id=3, quality="RECORDED", rule_version=1))
        s.add(TripStop(id=5, trip_id=3, stop_sequence=1, station_id=3, quality="RECORDED", rule_version=1))
        s.add(TripStop(id=6, trip_id=4, stop_sequence=1, station_id=1, quality="RECORDED", rule_version=1))
        s.add(PublicHoliday(date="2026-06-04", state="DE-NW", name="Fronleichnam", partial=0))
        s.add(PublicHoliday(date="2026-10-03", state="DE-NW", name="Tag der Deutschen Einheit", partial=0))
        for state in ALL_STATE_CODES:
            if state != "DE-NW":
                s.add(PublicHoliday(date="2026-10-03", state=state, name="Tag der Deutschen Einheit", partial=0))
        s.commit()
    return engine


def _labels(engine) -> dict[int, int | None]:
    with Session(engine, future=True) as s:
        rows = s.execute(select(TripStop.id, TripStop.is_holiday)).all()
    return dict(rows)


def test_state_specific_holiday_flags_only_its_own_state(engine):
    label_trip_stop_holidays(engine)
    labels = _labels(engine)
    assert labels[1] == 1  # NW station on Fronleichnam
    assert labels[2] == 0  # HH station on Fronleichnam (not observed there)


def test_unknown_state_on_a_sunday_is_a_holiday(engine):
    label_trip_stop_holidays(engine)
    labels = _labels(engine)
    assert labels[4] == 1


def test_a_plain_weekday_is_not_a_holiday(engine):
    label_trip_stop_holidays(engine)
    labels = _labels(engine)
    assert labels[6] == 0


def test_each_service_date_is_committed_and_reported(engine):
    seen = []
    touched = label_trip_stop_holidays(engine, progress=lambda d, n: seen.append((d, n)))
    assert seen == [("2026-06-04", 3), ("2026-09-15", 1), ("2026-09-20", 1), ("2026-10-03", 1)]
    assert touched == 6
