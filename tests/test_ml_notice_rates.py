"""NoticeRates: smoothed historical rates, leave-one-month-out folds, and the manifest check."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.ml.notice_rates import WEIGHT, NoticeRates, NoticeRatesMismatch
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


def test_a_train_with_no_history_is_missing_not_zero():
    engine = _engine([("station", 1, MARCH, 1000, 100, 0)])
    r = _at(_rates(engine), train=99)
    assert np.isnan(r["works_rate_train"]) and np.isnan(r["disruption_rate_train"])
    assert not np.isnan(r["works_rate_station"])


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


# -- the manifest record and its check (task 2.2) -------------------------------------------------


def test_changed_counts_are_refused_with_a_message_naming_the_dates():
    engine = _engine([("station", 1, MARCH, 100, 10, 5), ("station", 1, APRIL, 100, 20, 5)])
    record = _rates(engine).record()
    with engine.begin() as conn:
        conn.execute(text("UPDATE notice_rate_daily SET k_works = k_works + 1 WHERE service_date = :d"),
                     {"d": MARCH})
    with pytest.raises(NoticeRatesMismatch, match=r"2026-03-14 \.\. 2026-04-10 \(2 dates\)"):
        NoticeRates.from_record(engine, record)


# -- serving: several requests at once ------------------------------------------------------------


