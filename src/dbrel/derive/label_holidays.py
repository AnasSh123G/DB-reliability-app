"""Fill trip_stop.is_holiday from public_holiday and each stop's own station.

Set-based SQL, not a Python loop -- trip_stop is 14.9M+ rows nationally, and
the constraint on this change is explicit that bulk updates over it must be
SQL, not ORM objects one at a time.

One UPDATE per service date, each committed on its own. A single UPDATE ... FROM
over the whole table makes SQLite collect every affected row in a temp table
first, which `db.py`'s `temp_store=MEMORY` keeps in RAM: on the ~120M-row
national database that was OOM-killed at 8 GB (2026-10-01). Per date, the
working set is one day's rows, and an interrupted run keeps the dates it
finished.

The rule (holiday-calendar spec, plus the NULL case this module adds for the
unresolved-station edge the spec's "excluded from averaging" scenario
implies):

  1   if the run's service_date (its ORIGIN date -- day_type_of's convention,
      so an overnight run keeps one day type at every station) is a Sunday,
      OR (service_date, the stop's own station's state) is a non-partial
      public_holiday row.
  0   otherwise, when the station's state is known.
  NULL if the station's state is unresolved AND service_date is a
      state-specific holiday (observed in fewer than all 16 states) -- it
      genuinely cannot be decided without knowing which state the station is
      in. A Sunday or an all-16-states holiday is still decidable (1) even
      with an unresolved station, since neither depends on which state it's
      in.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

# The stop's own station's state, as a scalar subquery on the row being updated.
_STATE = "(SELECT state FROM station WHERE station.id = trip_stop.station_id)"

_LABEL_ONE_DATE = f"""
    UPDATE trip_stop
    SET is_holiday = CASE
        WHEN strftime('%w', :d) = '0' THEN 1
        WHEN {_STATE} IS NOT NULL THEN EXISTS (
            SELECT 1 FROM public_holiday ph
            WHERE ph.date = :d AND ph.state = {_STATE} AND ph.partial = 0
        )
        WHEN :nationwide THEN 1
        ELSE NULL
    END
    WHERE trip_id IN (SELECT id FROM trip WHERE service_date = :d)
"""

_NATIONWIDE = """
    SELECT date FROM public_holiday
    WHERE partial = 0
    GROUP BY date
    HAVING COUNT(DISTINCT state) = 16
"""


def label_trip_stop_holidays(engine: Engine,
                             progress: Callable[[str, int], None] | None = None) -> int:
    """(Re)compute trip_stop.is_holiday for every row. Re-runnable, deterministic.

    Returns the number of trip_stop rows touched (all of them, via rowcount).
    `progress(service_date, rows)` is called after each date is committed.
    """
    with engine.connect() as conn:
        nationwide = {d for (d,) in conn.exec_driver_sql(_NATIONWIDE)}
        dates = [d for (d,) in conn.exec_driver_sql(
            "SELECT DISTINCT service_date FROM trip WHERE service_date IS NOT NULL ORDER BY 1")]
    touched = 0
    for d in dates:
        with engine.begin() as conn:
            rows = conn.execute(text(_LABEL_ONE_DATE), {"d": d, "nationwide": int(d in nationwide)}).rowcount
        touched += rows
        if progress:
            progress(d, rows)
    return touched
