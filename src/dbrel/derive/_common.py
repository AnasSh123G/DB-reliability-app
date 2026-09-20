"""Pieces shared by every delay aggregate.

There is more than one way to group the same facts — by weekday, by day type and
scheduled time, and more to come — but there must only be one way to *measure*
them. If two aggregates computed their means or their punctuality cutoffs even
slightly differently, the two tables would disagree about the same railway and
neither number could be trusted. So the grouping lives in each aggregate module and
everything below the grouping lives here.

Nothing in this module knows what a key looks like.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.config import AVERAGES_RULE_VERSION, PUNCTUAL_S
from dbrel.models import Train

# One day of slack on the watermark, so a --force re-finalize that rewrites
# yesterday's grades is still picked up as dirty.
WATERMARK_SLACK_S = 86_400


@dataclass(slots=True)
class Samples:
    """Accumulator for one group, whatever that group is keyed on."""

    arr: list[int]
    dep: list[int]
    trips: set[int]
    cancelled: int = 0
    known: int = 0
    first_date: str | None = None
    last_date: str | None = None

    def observe_date(self, service_date: str | None) -> None:
        if not service_date:
            return
        if self.first_date is None or service_date < self.first_date:
            self.first_date = service_date
        if self.last_date is None or service_date > self.last_date:
            self.last_date = service_date


def new_samples() -> Samples:
    """defaultdict factory — the mutable fields need a fresh instance each time."""
    return Samples(arr=[], dep=[], trips=set())


def percentile(ordered: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile over an already-sorted list.

    SQLite has no percentile_cont, and sorting each group's samples in Python is
    exact, single-pass and needs no extension.
    """
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))
    return ordered[index]


def rate(values: list[int], threshold: int) -> float | None:
    """Share of values strictly below threshold. 359 s is punctual, 361 s is not."""
    if not values:
        return None
    return sum(1 for v in values if v < threshold) / len(values)


def train_attributes(
    session: Session, train_ids: Iterable[int]
) -> dict[int, tuple[int | None, str | None]]:
    """train_id -> (line_id, segment), denormalised onto every aggregate row."""
    ids = list(train_ids)
    if not ids:
        return {}
    return {
        train_id: (line_id, segment)
        for train_id, line_id, segment in session.execute(
            select(Train.id, Train.line_id, Train.segment).where(Train.id.in_(ids))
        )
    }


def metric_row(bucket: Samples, now: int) -> dict:
    """Every measured column of an aggregate row, given one group's samples.

    Pure function of the bucket: the caller adds its own key columns and whatever
    it denormalises. Counts are kept apart for arrival and departure because a stop
    can have one without the other — an origin has no arrival, a terminus no
    departure — and a single sample count would be wrong for the missing half.
    """
    arr = sorted(bucket.arr)
    dep = sorted(bucket.dep)
    return {
        "n_arr": len(arr),
        "n_dep": len(dep),
        "n_runs": len(bucket.trips),
        "n_cancelled": bucket.cancelled,
        "n_known": bucket.known,
        "avg_arr_delay_s": sum(arr) / len(arr) if arr else None,
        "avg_dep_delay_s": sum(dep) / len(dep) if dep else None,
        "p50_arr_delay_s": percentile(arr, 0.5),
        "p50_dep_delay_s": percentile(dep, 0.5),
        "p90_arr_delay_s": percentile(arr, 0.9),
        "p90_dep_delay_s": percentile(dep, 0.9),
        "max_dep_delay_s": dep[-1] if dep else None,
        "pct_arr_punctual": rate(arr, PUNCTUAL_S),
        "pct_dep_punctual": rate(dep, PUNCTUAL_S),
        "first_service_date": bucket.first_date,
        "last_service_date": bucket.last_date,
        "computed_at": now,
        "rule_version": AVERAGES_RULE_VERSION,
    }
