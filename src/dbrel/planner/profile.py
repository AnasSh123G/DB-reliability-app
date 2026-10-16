"""Shared running-profile aggregation for collection and deployed databases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text

from dbrel.holidays_de import is_holiday
from dbrel.planner.constants import PROFILE_DAYS, PROFILE_MIN_RUNS

PROFILE_SQL = """
SELECT t.train_id, t.path_sha1, (CAST(strftime('%w', t.service_date) AS INTEGER) + 6) % 7 AS weekday,
       COUNT(*) AS n_runs
FROM {trip} t
WHERE t.service_date BETWEEN :first AND :last
  AND t.service_date NOT IN (SELECT h.date FROM {holiday} h WHERE h.partial = 0)
  AND t.train_id IN (SELECT r.id FROM {train} r WHERE r.last_seen_date IS NULL OR r.last_seen_date >= :cutoff)
GROUP BY t.train_id, t.path_sha1, weekday
ORDER BY t.train_id, t.path_sha1, weekday
"""

NOTHING = object()


@dataclass(frozen=True)
class RunProfile:
    counts: dict[tuple[int, str | None], tuple[int, int, int, int, int, int, int]]
    first_date: date | None
    last_date: date | None


def profile_params(conn, trip: str, train: str) -> dict | None:
    """Return the profile window for trusted, internal SQL table names."""
    # Local import keeps the planner independent of web queries at import time.
    from dbrel.web.queries import ACTIVE_WINDOW_DAYS

    def maximum(column, table):
        sql = f"SELECT MAX({column}) FROM {table}"
        if hasattr(conn, "exec_driver_sql"):
            return conn.exec_driver_sql(sql).scalar_one()
        return conn.execute(sql).fetchone()[0]

    last = maximum("service_date", trip)
    if last is None:
        return None
    first = date.fromisoformat(last) - timedelta(days=PROFILE_DAYS - 1)
    newest = maximum("last_seen_date", train)
    cutoff = (
        (date.fromisoformat(newest) - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat()
        if newest
        else None
    )
    return {"first": first.isoformat(), "last": last, "cutoff": cutoff}


def load_run_profile(session) -> RunProfile:
    """Prefer the deployed aggregate, otherwise aggregate collection trips."""
    names = set(
        session.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
                "AND name IN ('train_variant_weekday', 'trip')"
            )
        ).scalars()
    )
    if "train_variant_weekday" in names:
        rows = session.execute(
            text(
                "SELECT train_id, path_sha1, weekday, n_runs FROM train_variant_weekday"
            )
        )
        window = session.execute(
            text("SELECT first_date, last_date FROM running_profile_window")
        ).first()
        first, last = window if window else (None, None)
    elif "trip" in names:
        params = profile_params(session.connection(), "trip", "train")
        if params is None:
            return RunProfile({}, None, None)
        rows = session.execute(
            text(
                PROFILE_SQL.format(trip="trip", holiday="public_holiday", train="train")
            ),
            params,
        )
        first, last = params["first"], params["last"]
    else:
        return RunProfile({}, None, None)
    counts = {}
    for train_id, path, weekday, n_runs in rows:
        key = (train_id, path)
        previous = counts.get(key, (0, 0, 0, 0, 0, 0, 0))
        counts[key] = previous[:weekday] + (n_runs,) + previous[weekday + 1 :]
    return RunProfile(
        counts,
        date.fromisoformat(first) if first else None,
        date.fromisoformat(last) if last else None,
    )


def day_key(
    service_date: date, state: str | None, holiday_set: set[tuple[str, str]]
) -> int:
    """Resolve the service weekday at the boarding station."""
    return 6 if is_holiday(service_date, state, holiday_set) else service_date.weekday()


def chosen_variants(profile: RunProfile) -> dict[int, list]:
    """Choose each train's strongest qualifying variant separately per weekday."""
    grouped = {}
    for (train_id, path), counts in profile.counts.items():
        grouped.setdefault(train_id, []).append((path, counts))
    return {
        train_id: [
            max(
                (
                    (counts[k], sum(counts), path or "", path)
                    for path, counts in variants
                    if counts[k] >= PROFILE_MIN_RUNS
                ),
                default=(0, 0, "", NOTHING),
                key=lambda candidate: candidate[:3],
            )[3]
            for k in range(7)
        ]
        for train_id, variants in grouped.items()
    }
