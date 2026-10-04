"""NoticeRates: smoothed historical rates, leave-one-month-out folds, and the manifest check."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.ml.notice_rates import RATE_NAMES, WEIGHT, NoticeRates, NoticeRatesMismatch, month_index
from dbrel.models import NoticeRateDaily

MARCH, APRIL = "2026-03-14", "2026-04-10"
VALIDATION = "2026-09-01"  # not a training date


def _day(date):
    return int(np.datetime64(date, "D").astype("int64"))


def _engine(rows):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add_all(NoticeRateDaily(level=lvl, key_id=k, service_date=d, n=n, k_works=w, k_disruption=x)
                  for lvl, k, d, n, w, x in rows)
        s.commit()
    return engine


def _same(a, b):
    assert a.keys() == b.keys()
    for name in a:
        np.testing.assert_allclose(a[name], b[name], equal_nan=True, err_msg=name)


def _rates(engine, dates=(MARCH, APRIL), **kw):
    return NoticeRates.from_database(engine, list(dates), **kw)


def _at(rates, *, station=1, line=-1, train=1, date=MARCH, fold=False):
    out = rates.rates([station], [line], [train], [_day(date)], fold=fold)
    return {name: float(v[0]) for name, v in out.items()}


def test_a_busy_stations_rate_is_its_share_of_arrivals():
    engine = _engine([
        ("station", 1, MARCH, 5000, 0, 600), ("station", 1, APRIL, 5000, 0, 600),
        ("station", 2, MARCH, 10000, 0, 0),
    ])
    r = _at(_rates(engine))
    overall = 1200 / 20000
    assert r["disruption_rate_station"] == pytest.approx((1200 + WEIGHT * overall) / (10000 + WEIGHT), abs=1e-6)
    assert r["disruption_rate_station"] == pytest.approx(0.12, abs=0.001)
    assert r["works_rate_station"] == pytest.approx(0.0)


def test_a_rarely_seen_train_stays_near_its_line():
    engine = _engine([
        ("station", 1, MARCH, 1000, 100, 0),
        ("line", 5, MARCH, 1000, 100, 0),
        ("train", 7, MARCH, 3, 3, 0),
    ])
    r = _at(_rates(engine), line=5, train=7)
    assert r["works_rate_train"] < 0.5
    assert abs(r["works_rate_train"] - 0.1) < abs(r["works_rate_train"] - 1.0)
    assert r["works_rate_line"] == pytest.approx(0.1, abs=0.001)


def test_a_train_with_no_history_is_missing_not_zero():
    engine = _engine([("station", 1, MARCH, 1000, 100, 0)])
    r = _at(_rates(engine), train=99)
    assert np.isnan(r["works_rate_train"]) and np.isnan(r["disruption_rate_train"])
    assert not np.isnan(r["works_rate_station"])


def test_a_train_without_a_line_has_a_missing_line_rate_and_uses_the_station():
    engine = _engine([
        ("station", 1, MARCH, 1000, 200, 0),
        ("train", 7, MARCH, 50, 10, 0),
    ])
    r = _at(_rates(engine), line=-1, train=7)
    assert np.isnan(r["works_rate_line"])
    # parent of the train is the station (rate 0.2): (10 + 20 * 0.2) / 70
    assert r["works_rate_train"] == pytest.approx(14 / 70, abs=1e-4)


def test_an_unknown_station_is_missing_and_a_line_borrows_the_overall_rate():
    engine = _engine([("station", 1, MARCH, 1000, 100, 0), ("line", 5, MARCH, 10, 5, 0)])
    r = _at(_rates(engine), station=42, line=5)
    assert np.isnan(r["works_rate_station"])
    assert r["works_rate_line"] == pytest.approx((5 + WEIGHT * 0.1) / (10 + WEIGHT), abs=1e-4)


def test_a_training_row_does_not_count_its_own_month():
    base = [("station", 1, MARCH, 100, 10, 10), ("station", 1, APRIL, 100, 50, 50)]
    extreme_march = base + [("station", 1, "2026-03-20", 1000, 1000, 1000)]
    dates = (MARCH, "2026-03-20", APRIL)
    plain = _at(_rates(_engine(base), dates), date=MARCH, fold=True)
    shifted = _at(_rates(_engine(extreme_march), dates), date=MARCH, fold=True)
    _same(plain, shifted)  # March arrivals, however extreme, are invisible to a March row
    april = _at(_rates(_engine(base), dates), date=APRIL, fold=True)
    assert april["works_rate_station"] != plain["works_rate_station"]  # a different month's fold
    assert plain["works_rate_station"] == pytest.approx((50 + WEIGHT * 0.5) / (100 + WEIGHT), abs=1e-4)  # April only


def test_validation_and_prediction_rows_use_every_training_date():
    base = [("station", 1, MARCH, 100, 10, 0), ("station", 1, APRIL, 100, 50, 0)]
    r = _at(_rates(_engine(base)), date=VALIDATION, fold=False)
    assert r["works_rate_station"] == pytest.approx((60 + WEIGHT * 0.3) / (200 + WEIGHT), abs=1e-4)


def test_dates_outside_the_training_dates_never_contribute():
    base = [("station", 1, MARCH, 100, 10, 0)]
    outside = base + [("station", 1, VALIDATION, 9999, 9999, 9999), ("station", 1, "2026-02-01", 9999, 9999, 0)]
    _same(_at(_rates(_engine(base)), date=VALIDATION), _at(_rates(_engine(outside)), date=VALIDATION))


def test_a_date_inside_the_range_but_not_listed_is_ignored():
    rows = [("station", 1, MARCH, 100, 10, 0), ("station", 1, "2026-03-20", 100, 100, 0),
            ("station", 1, APRIL, 100, 10, 0)]
    listed = _at(_rates(_engine(rows), (MARCH, APRIL)), date=VALIDATION)
    assert listed["works_rate_station"] == pytest.approx((20 + WEIGHT * 0.1) / (200 + WEIGHT), abs=1e-4)


def test_rows_are_computed_together_across_months():
    engine = _engine([("station", 1, MARCH, 100, 10, 0), ("station", 1, APRIL, 100, 50, 0)])
    rates = _rates(engine)
    day = [_day(MARCH), _day(APRIL), _day(MARCH)]
    out = rates.rates([1, 1, 1], [-1, -1, -1], [1, 1, 1], day, fold=True)
    assert out["works_rate_station"][0] == out["works_rate_station"][2] != out["works_rate_station"][1]
    assert set(out) == set(RATE_NAMES) and all(v.dtype == np.float32 for v in out.values())
    assert list(month_index([_day(MARCH), _day(APRIL)])) == [674, 675]  # 2026-03, 2026-04


def test_no_counted_arrivals_is_an_error():
    with pytest.raises(ValueError, match="build-notice-rates"):
        _rates(_engine([("station", 1, VALIDATION, 10, 1, 1)]))


# -- the manifest record and its check (task 2.2) -------------------------------------------------

def test_a_saved_record_rebuilds_identical_rates():
    engine = _engine([("station", 1, MARCH, 100, 10, 5), ("train", 3, APRIL, 40, 4, 2)])
    original = _rates(engine)
    record = original.record()
    assert set(record) == {"dates", "weight", "fingerprint"} and record["dates"] == [MARCH, APRIL]
    again = NoticeRates.from_record(engine, record)
    _same(_at(again, train=3, date=APRIL), _at(original, train=3, date=APRIL))


def test_changed_counts_are_refused_with_a_message_naming_the_dates():
    engine = _engine([("station", 1, MARCH, 100, 10, 5), ("station", 1, APRIL, 100, 20, 5)])
    record = _rates(engine).record()
    with engine.begin() as conn:
        conn.execute(text("UPDATE notice_rate_daily SET k_works = k_works + 1 WHERE service_date = :d"),
                     {"d": MARCH})
    with pytest.raises(NoticeRatesMismatch, match=r"2026-03-14 \.\. 2026-04-10 \(2 dates\)"):
        NoticeRates.from_record(engine, record)


def test_rows_outside_the_recorded_dates_do_not_trip_the_check():
    engine = _engine([("station", 1, MARCH, 100, 10, 5)])
    record = _rates(engine, dates=(MARCH,)).record()
    with Session(engine, future=True) as s:
        s.add(NoticeRateDaily(level="station", key_id=1, service_date=VALIDATION, n=5, k_works=5, k_disruption=5))
        s.commit()
    NoticeRates.from_record(engine, record)  # no error: only the validation date changed
