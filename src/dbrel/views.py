"""Named views over the raw facts.

One SQL view per day of the week, over the dated facts. It exists so a join or a
filter that every reader would otherwise rewrite gets written once and given a
name that reads like a table.

train_stop_average and train_station_delay used to have a second family of views
here (avg_weekday, avg_sunday_holiday) splitting the aggregate by day type. Both
tables were dropped — lateness figures are now scored live by ml/live.py rather
than read back from a precomputed aggregate — so those views went with them.

trip_stop is where every recorded time lands, and it carries no date of its own —
it is reached through trip_id, and trip.service_date is what says which day a run
belongs to. That join is the thing anyone slicing the data by day has to write
over and over, so it is written once here and given seven names.

`SELECT * FROM day_monday` is then exactly "every stop of every run that departed
its origin on a Monday", including the service_date it came from, so a query can
still tell one Monday from another.

Weekday numbering is a genuine trap: SQLite's strftime('%w') is 0=Sunday, while
Python's date.weekday() -- which is what train_station_delay.dow stores -- is
0=Monday. The two are converted deliberately below rather than left to coincide,
and tests/test_views.py pins them against each other for a known date.
"""


from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Index is Python's weekday(): 0=Monday..6=Sunday.
DAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def sqlite_dow_expr(python_dow: int) -> int:
    """Python's 0=Monday..6=Sunday to SQLite's strftime('%w') 0=Sunday..6=Saturday."""
    return (python_dow + 1) % 7


def day_view_sql(python_dow: int) -> str:
    name = DAY_NAMES[python_dow]
    return (
        f"CREATE VIEW IF NOT EXISTS day_{name} AS "
        "SELECT t.service_date AS service_date, t.train_id AS train_id, "
        "t.source AS source, t.status AS status, ts.* "
        "FROM trip_stop ts JOIN trip t ON t.id = ts.trip_id "
        f"WHERE CAST(strftime('%w', t.service_date) AS INTEGER) = {sqlite_dow_expr(python_dow)}"
    )


def create_day_views(engine: Engine) -> None:
    """Create the seven day views. Idempotent, so it can run on every create_all()."""
    with engine.begin() as connection:
        for python_dow in range(7):
            connection.execute(text(day_view_sql(python_dow)))


def drop_day_views(engine: Engine) -> None:
    with engine.begin() as connection:
        for name in DAY_NAMES:
            connection.execute(text(f"DROP VIEW IF EXISTS day_{name}"))
