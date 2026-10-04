"""Fill notice_rate_daily: per-day counts of arrivals on trips with a notice, per key.

The notice-rate model inputs (`ml/notice_rates.py`) are sums of these rows. A "notice" is a
`tt_message` row of type 'h' whose category starts with "Bauarbeiten" (construction) or
"Störung"/"Großstörung" (disruption); it counts for a trip when the trip has at least one
non-deleted version of it. Only arrivals the model trains on are counted: the filter is the
dataset's own (`ml.dataset._where`), so "eligible" cannot drift from what training sees.

One service date per transaction, replacing that date's rows, so memory stays at one day's arrivals
(about 140 MB on the national database) and an interrupted build only repeats unfinished dates.
Reads trip, trip_stop, train, station and tt_message; writes only notice_rate_daily.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable
from datetime import date

from sqlalchemy import delete, insert
from sqlalchemy.engine import Engine

from dbrel.ml.dataset import MONTH_FROM, _where
from dbrel.ml.features import NOTICE_LEVELS as LEVELS  # noqa: F401 - the levels the table holds
from dbrel.models import NoticeRateDaily

WORKS_PREFIX = "Bauarbeiten"
DISRUPTION_PREFIXES = ("Störung", "Großstörung")
SPARSE_MONTH_FACTOR = 3  # a month this many times below the median notice density is flagged

_ARRIVALS = f"""
    SELECT ts.trip_id, ts.station_id, tr.line_id, tr.id
    {MONTH_FROM}
    JOIN train tr ON tr.id = t.train_id
    JOIN station st ON st.id = ts.station_id
    {_where(arrival_only=True)}
      AND t.service_date = ?
"""
_NOTICES = """
    SELECT DISTINCT m.trip_id, m.msg_id, m.category
    FROM trip t CROSS JOIN tt_message m ON m.trip_id = t.id
    WHERE t.service_date = ? AND m.msg_type = 'h' AND m.deleted = 0 AND m.category IS NOT NULL
"""


def notice_kind(category: str | None) -> str | None:
    """'works', 'disruption' or None for a message category."""
    if not category:
        return None
    if category.startswith(WORKS_PREFIX):
        return "works"
    if category.startswith(DISRUPTION_PREFIXES):
        return "disruption"
    return None


def _day_rows(engine: Engine, service_date: str) -> tuple[list[dict], dict]:
    """The rows to store for one date, and its trip and notice totals."""
    with engine.connect() as conn:
        flags: dict[int, list[bool]] = defaultdict(lambda: [False, False])
        notices = {"works": set(), "disruption": set()}
        for trip_id, msg_id, category in conn.exec_driver_sql(_NOTICES, (service_date,)):
            kind = notice_kind(category)
            if kind is not None:
                flags[trip_id][kind == "disruption"] = True
                notices[kind].add((trip_id, msg_id))
        counts: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0, 0])
        trips: set[int] = set()
        for trip_id, station_id, line_id, train_id in conn.exec_driver_sql(_ARRIVALS, (service_date,)):
            trips.add(trip_id)
            works, disruption = flags.get(trip_id, (False, False))
            keys = [("station", station_id), ("train", train_id)]
            if line_id is not None:
                keys.append(("line", line_id))
            for key in keys:
                c = counts[key]
                c[0] += 1
                c[1] += works
                c[2] += disruption
    rows = [{"level": level, "key_id": key_id, "service_date": service_date,
             "n": n, "k_works": k_works, "k_disruption": k_disruption}
            for (level, key_id), (n, k_works, k_disruption) in counts.items()]
    on_trips = {kind: sum(1 for t, _ in pairs if t in trips) for kind, pairs in notices.items()}
    return rows, {"trips": len(trips), "arrivals": sum(c[0] for (level, _), c in counts.items() if level == "station"),
                  "works_notices": on_trips["works"], "disruption_notices": on_trips["disruption"]}


def build_notice_rates(engine: Engine, *, first: str | None = None, last: str | None = None,
                       progress: Callable[[str, int], None] | None = None) -> dict[str, dict]:
    """(Re)build the table for the service dates in [first, last]; returns totals per month.

    Idempotent: a date's rows are replaced, never added to.
    """
    NoticeRateDaily.__table__.create(engine, checkfirst=True)  # an existing database may predate the table
    with engine.connect() as conn:
        dates = [d for (d,) in conn.exec_driver_sql(
            "SELECT DISTINCT service_date FROM trip WHERE service_date IS NOT NULL ORDER BY 1")]
    dates = [d for d in dates if (first is None or d >= first) and (last is None or d <= last)]
    months: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for service_date in dates:
        date.fromisoformat(service_date)  # a malformed date should fail loudly, not be skipped
        rows, totals = _day_rows(engine, service_date)
        with engine.begin() as conn:
            conn.execute(delete(NoticeRateDaily).where(NoticeRateDaily.service_date == service_date))
            if rows:
                conn.execute(insert(NoticeRateDaily), rows)
        for name, value in totals.items():
            months[service_date[:7]][name] += value
        if progress:
            progress(service_date, len(rows))
    return {month: dict(totals) for month, totals in sorted(months.items())}


def notices_per_1000_trips(totals: dict) -> float:
    """Construction plus disruption notices per 1,000 trips of a month's totals."""
    return 1000 * (totals["works_notices"] + totals["disruption_notices"]) / totals["trips"] if totals["trips"] else 0.0


def sparse_months(summary: dict[str, dict]) -> list[str]:
    """Months whose notice density is more than SPARSE_MONTH_FACTOR times below the median."""
    density = {m: notices_per_1000_trips(t) for m, t in summary.items() if t["trips"]}
    if len(density) < 3:
        return []
    median = statistics.median(density.values())
    return [m for m, d in density.items() if d * SPARSE_MONTH_FACTOR < median]
