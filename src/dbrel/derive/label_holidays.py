"""Fill trip_stop.is_holiday from public_holiday and each stop's own station.

Set-based SQL, not a Python loop -- trip_stop is 14.9M+ rows nationally, and
the constraint on this change is explicit that bulk updates over it must be
SQL, not ORM objects one at a time.

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

from sqlalchemy import text
from sqlalchemy.engine import Engine


def label_trip_stop_holidays(engine: Engine) -> int:
    """(Re)compute trip_stop.is_holiday for every row. Re-runnable, deterministic.

    Returns the number of trip_stop rows touched (all of them, via rowcount).
    """
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS holiday_nationwide")
        conn.exec_driver_sql(
            """
            CREATE TEMP TABLE holiday_nationwide AS
            SELECT date FROM public_holiday
            WHERE partial = 0
            GROUP BY date
            HAVING COUNT(DISTINCT state) = 16
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX idx_holiday_nationwide_date ON holiday_nationwide(date)"
        )

        result = conn.execute(
            text(
                """
                UPDATE trip_stop
                SET is_holiday = CASE
                    WHEN strftime('%w', trip.service_date) = '0' THEN 1
                    WHEN station.state IS NOT NULL AND EXISTS (
                        SELECT 1 FROM public_holiday ph
                        WHERE ph.date = trip.service_date
                          AND ph.state = station.state
                          AND ph.partial = 0
                    ) THEN 1
                    WHEN station.state IS NOT NULL THEN 0
                    WHEN EXISTS (
                        SELECT 1 FROM holiday_nationwide hn
                        WHERE hn.date = trip.service_date
                    ) THEN 1
                    ELSE NULL
                END
                FROM trip, station
                WHERE trip.id = trip_stop.trip_id
                  AND station.id = trip_stop.station_id
                """
            )
        )
        touched = result.rowcount
        conn.exec_driver_sql("DROP TABLE IF EXISTS holiday_nationwide")
    return touched
