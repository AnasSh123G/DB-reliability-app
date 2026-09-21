"""Feature extraction and the temporal split.

The leakage test in here is the most important test in the model pipeline: a
model fed its own label scores beautifully and predicts nothing, and that failure
looks exactly like success on every metric. Everything else in this file guards
the split, which is the other way to get a flattering number from nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.ml._common import (
    CATEGORICAL_INDEXES,
    FEATURE_NAMES,
    FEATURE_SPEC,
    LEAKAGE_COLUMNS,
)
from dbrel.ml.features import (
    UNKNOWN_CODE,
    build_vocabularies,
    encode,
    extract,
    split_by_date,
)
from dbrel.models import (
    DAY_TYPE_SUNDAY_HOLIDAY,
    DAY_TYPE_WEEKDAY,
    PublicHoliday,
    Station,
    Train,
    Trip,
    TripStop,
)

# 2026-09-07 is a Monday, so this is two clean weeks Mon..Sun.
DATES = [f"2026-09-{day:02d}" for day in range(7, 21)]
TRAIN_ID, STATION_ID = 500, 1


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(Station(id=STATION_ID, natural_key="bochum", name="Bochum Hbf",
                      lat=51.48, lon=7.22, tracked=1))
        s.add(Station(id=2, natural_key="essen", name="Essen Hbf",
                      lat=51.45, lon=7.01, tracked=1))
        s.add(Train(id=TRAIN_ID, source=SOURCE_TT, identity_key="tts:RE/1",
                    category="RE", segment="RV", train_number=1, operator="DB",
                    display_name="RE 1", n_stops=4, route_path="A|B|C|D",
                    first_departure_sec=8 * 3600))
        s.flush()
        for index, date in enumerate(DATES):
            trip = Trip(id=index + 1, train_id=TRAIN_ID, service_date=date,
                        source=SOURCE_TT, source_run_key=f"k{index}",
                        status="COMPLETED", endpoints_tracked=1, n_polls_seen=1)
            s.add(trip)
            s.flush()
            # 08:10 local on that date, as an epoch; exact value does not matter
            # to these tests, only that it is consistent per date.
            base = 1789000000 + index * 86400
            s.add(TripStop(trip_id=trip.id, stop_sequence=2, station_id=STATION_ID,
                           supposed_arrival_utc=base, supposed_departure_utc=base + 60,
                           actual_departure_utc=base + 120, dep_delay_s=60 + index,
                           arr_delay_s=60 + index, quality="RECORDED", rule_version=1,
                           cancelled=0, arr_cancelled=0, dep_cancelled=0, added=0,
                           is_handover=0, planned_platform="3"))
        s.flush()
        yield s


class TestLeakage:
    def test_no_feature_is_derived_from_a_fact_that_only_exists_afterwards(self):
        """The one that matters.

        A model given dep_delay_s, or the actual times, or the quality grade,
        scores near-perfectly and can predict nothing, because none of those
        exist at the moment a prediction is wanted.
        """
        sources = {f.source for f in FEATURE_SPEC}
        assert sources & LEAKAGE_COLUMNS == set()

    def test_the_target_itself_is_on_the_forbidden_list(self):
        """Guards the guard: an empty LEAKAGE_COLUMNS would pass the test above."""
        assert "dep_delay_s" in LEAKAGE_COLUMNS
        assert "actual_departure_utc" in LEAKAGE_COLUMNS

    def test_changed_platform_is_treated_as_leakage(self):
        """It reads as a planned field but IRIS publishes it in the last hour
        before departure, and a platform change is a symptom of disruption."""
        assert "changed_platform" in LEAKAGE_COLUMNS


class TestExtract:
    def test_every_row_has_exactly_the_declared_features(self, session):
        data = extract(session)
        assert len(data) == len(DATES)
        assert all(len(row) == len(FEATURE_SPEC) for row in data.rows)

    def test_label_is_the_departure_delay(self, session):
        data = extract(session)
        assert sorted(data.labels) == [float(60 + i) for i in range(len(DATES))]

    def test_key_matches_the_aggregate_key_shape(self, session):
        """Predictions are materialised at train_stop_average's key, so the
        tuple built here has to be the same four things in the same order."""
        data = extract(session)
        day_type, train_id, station_id, sched_sec = data.keys[0]
        assert day_type in (DAY_TYPE_WEEKDAY, DAY_TYPE_SUNDAY_HOLIDAY)
        assert (train_id, station_id) == (TRAIN_ID, STATION_ID)
        assert isinstance(sched_sec, int)

    def test_handover_stops_are_excluded(self, session):
        session.query(TripStop).filter_by(trip_id=1).one().is_handover = 1
        session.flush()
        assert len(extract(session)) == len(DATES) - 1

    def test_unrecorded_stops_are_excluded(self, session):
        """NO_DATA is an unknown, not a zero — the same filter the aggregates use."""
        session.query(TripStop).filter_by(trip_id=1).one().quality = "NO_DATA"
        session.flush()
        assert len(extract(session)) == len(DATES) - 1

    def test_a_stop_with_no_delay_is_excluded(self, session):
        session.query(TripStop).filter_by(trip_id=1).one().dep_delay_s = None
        session.flush()
        assert len(extract(session)) == len(DATES) - 1

    def test_the_window_is_respected(self, session):
        data = extract(session, since="2026-09-14")
        assert set(data.dates) == set(DATES[7:])

    def test_day_type_comes_from_the_holiday_table(self, session):
        """A midweek date with a holiday row must be graded as a Sunday."""
        session.add(PublicHoliday(date="2026-09-09", name="Test"))
        session.flush()
        by_date = dict(zip(extract(session).dates, [k[0] for k in extract(session).keys]))
        assert by_date["2026-09-09"] == DAY_TYPE_SUNDAY_HOLIDAY
        assert by_date["2026-09-10"] == DAY_TYPE_WEEKDAY


class TestSplit:
    def test_the_three_periods_share_no_date(self, session):
        split = split_by_date(extract(session).dates)
        assert set(split.fit_dates).isdisjoint(split.calibrate_dates)
        assert set(split.fit_dates).isdisjoint(split.test_dates)
        assert set(split.calibrate_dates).isdisjoint(split.test_dates)

    def test_each_period_is_strictly_later_than_the_last(self, session):
        """The whole reason for splitting this way: test must be the future."""
        split = split_by_date(extract(session).dates)
        assert max(split.fit_dates) < min(split.calibrate_dates)
        assert max(split.calibrate_dates) < min(split.test_dates)

    def test_every_row_lands_in_exactly_one_period(self, session):
        data = extract(session)
        split = split_by_date(data.dates)
        allocated = split.fit + split.calibrate + split.test
        assert sorted(allocated) == list(range(len(data)))

    def test_a_single_date_still_produces_a_usable_split(self):
        """Degenerate but must not crash — the pipeline has to report, not raise."""
        split = split_by_date(["2026-09-07"] * 5)
        assert len(split.fit) + len(split.calibrate) + len(split.test) == 5


class TestVocabularies:
    def test_a_category_seen_only_after_the_split_is_unknown(self, session):
        """A vocabulary built over the test period would be a quiet leak.

        The model must meet a train it has never seen as an unknown, exactly as
        it will in production the day a new service is added.
        """
        data = extract(session)
        split = split_by_date(data.dates)
        position = FEATURE_NAMES.index("train_id")
        for index in split.test:
            data.rows[index][position] = 9999

        vocabularies = build_vocabularies(data, split.fit)
        encoded = encode(data, vocabularies)
        assert encoded[split.test[0]][position] == UNKNOWN_CODE
        assert encoded[split.fit[0]][position] != UNKNOWN_CODE

    def test_every_categorical_feature_gets_a_vocabulary(self, session):
        data = extract(session)
        vocabularies = build_vocabularies(data, list(range(len(data))))
        assert set(vocabularies) == {FEATURE_NAMES[i] for i in CATEGORICAL_INDEXES}

    def test_encoding_is_numeric_throughout(self, session):
        data = extract(session)
        vocabularies = build_vocabularies(data, list(range(len(data))))
        for row in encode(data, vocabularies):
            assert all(isinstance(v, float) for v in row)
