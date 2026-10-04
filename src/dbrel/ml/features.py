"""The delay model's features: one definition, used by training and prediction alike.

**Every feature is knowable before the train runs.** Identity, calendar,
scheduled time, trip start time, position on the route, the station's place on
the map, and direction of travel -- all read off the timetable, the calendar and
the station table. Nothing comes from a run's own observed times, delays,
cancellations, changed platforms or disruption messages, and nothing from any
other run (`LIVE_ONLY_COLUMNS` names what is forbidden; a test checks it).

**One function turns raw inputs into feature columns** -- `compute_features`.
The dataset builder calls it on eleven million rows at once, the prediction
command on a handful; both hand it the same `RAW_COLUMNS`. Two separate
implementations would drift, and a model indexes features by position, so it
would keep predicting -- confidently, from the wrong columns -- with nothing to
raise an error.

**Categoricals are encoded against a vocabulary fixed at training time.** A
value the vocabulary has never seen (a train added after training, a newly
tracked station) becomes `UNKNOWN`, and the model falls back on everything else
it knows about the call. "No line" (ICE, IC, ...) is not unknown: it is its own
category, `NO_LINE`.

Direction for a train with a line is `direction.direction_by_train_id`, the
same split the web views and the averages use. A line-less train has no line
to measure against, so it is measured against the longest route among the
trains of its kind on its stretch (`corridor_direction`). Measuring it against
its *own* route, as the search view's corridor does for the chosen train, would
make every line-less train direction 0 and the feature worthless.
"""

from __future__ import annotations

from datetime import date

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.direction import corridor_members, direction_by_train_id, direction_of
from dbrel.models import ServiceLine, Train

# What a caller supplies per call. Training reads these from trip_stop and
# friends; prediction from station_stop and friends.
RAW_COLUMNS = (
    "station_id",
    "train_id",
    "line_id",  # NO_LINE for a train without one
    "category",
    "operator",
    "state",  # "" when the station's state is unresolved
    "lat",
    "lon",
    "weekday",  # 0 = Monday, of the service (origin) date
    "is_holiday",  # 0/1, holiday-calendar rule for the station's state
    "sched_local_sec",  # local seconds of COALESCE(departure, arrival); may exceed 86400
    "trip_start_sec",  # local seconds of the trip's origin departure; NaN if unknown
    "stop_rank",  # 0-based position within the route variant; NaN if unknown
    "n_stops",  # stops in that route variant; NaN if unknown
    "direction",  # 0/1, UNKNOWN_DIRECTION if unresolved
    "day_of_year_idx",  # service date's month/day on a 366-day calendar, 0 = 1 Jan; see day_of_year_index
)

CATEGORICAL = ("station", "train", "line", "category", "operator", "state", "weekday", "direction")
NUMERIC = (
    "is_holiday",
    "sched_min",
    "trip_start_min",
    "minutes_since_start",
    "stop_frac",
    "n_stops",
    "is_origin",
    "is_terminus",
    "lat",
    "lon",
)
# Time of year as one point on a circle. Only the arrival models trained with the full
# history use it; the v1/v2 artifacts keep their trained layout (NUMERIC/FEATURES).
SEASONAL = ("doy_sin", "doy_cos")
ARRIVAL_NUMERIC = NUMERIC + SEASONAL
# Historical notice rates of the station, line and train (see ml/notice_rates.py). They are looked up
# from stored counts, not computed from a call's raw columns, so `numeric_features` does not produce
# them; a model lists them in its `features_numeric` and the caller supplies the columns.
NOTICE_LEVELS = ("station", "line", "train")
NOTICE_RATES = tuple(f"{kind}_rate_{level}" for kind in ("works", "disruption") for level in NOTICE_LEVELS)
NOTICE_NUMERIC = ARRIVAL_NUMERIC + NOTICE_RATES
FEATURES = CATEGORICAL + NUMERIC
DAYS_IN_CYCLE = 366
# Days before each month in a leap year: the same month/day gets the same index every year.
_MONTH_OFFSET = np.array([0, 31, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335])

# Which raw column each categorical feature is encoded from.
_CATEGORICAL_SOURCE = {
    "station": "station_id",
    "train": "train_id",
    "line": "line_id",
    "category": "category",
    "operator": "operator",
    "state": "state",
    "weekday": "weekday",
    "direction": "direction",
}

UNKNOWN = -1
NO_LINE = -1
UNKNOWN_DIRECTION = -1

# Columns of the fact tables that exist only once a run has happened. None of
# them may ever feed a feature (spec: "A live-only field is not a feature").
LIVE_ONLY_COLUMNS = frozenset({
    "actual_arrival_utc",
    "actual_departure_utc",
    "arr_delay_s",
    "dep_delay_s",
    "cancelled",
    "arr_cancelled",
    "dep_cancelled",
    "cancelled_at_utc",
    "changed_platform",
    "quality",
    "last_observed_at",
    "poll_gap_s",
    "read_lag_s",
    "added",
    "final_delay_s",
    "max_delay_s",
    "msg_id",
    "code",
})


def compute_features(raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Raw per-call inputs -> feature columns (categoricals still un-encoded).

    Numeric columns come back as float32 with NaN for "unknown"; categorical
    ones as the raw values `encode_categoricals` maps through a vocabulary.
    """
    n = len(raw["station_id"])
    out = numeric_features(raw)
    for feature, source in _CATEGORICAL_SOURCE.items():
        values = raw[source]
        if source in ("category", "operator", "state"):
            values = np.array(["" if v is None else str(v) for v in values], dtype=object)
        else:
            values = np.asarray(values, dtype=np.int64)
        out[feature] = values
    assert all(len(v) == n for v in out.values())
    return out


# The raw columns the numeric features are computed from.
NUMERIC_SOURCES = ("is_holiday", "sched_local_sec", "trip_start_sec", "stop_rank", "n_stops",
                   "lat", "lon", "day_of_year_idx")


def numeric_features(raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """The `ARRIVAL_NUMERIC` columns of `compute_features`, from `NUMERIC_SOURCES` alone."""
    sched_sec = np.asarray(raw["sched_local_sec"], dtype=np.float64) % 86400
    sched_min = np.floor(sched_sec / 60)
    start_sec = np.asarray(raw["trip_start_sec"], dtype=np.float64) % 86400
    start_min = np.floor(start_sec / 60)
    rank = np.asarray(raw["stop_rank"], dtype=np.float64)
    n_stops = np.asarray(raw["n_stops"], dtype=np.float64)

    with np.errstate(invalid="ignore", divide="ignore"):
        stop_frac = np.where(n_stops > 1, rank / (n_stops - 1), 0.0)
    stop_frac = np.where(np.isnan(rank) | np.isnan(n_stops), np.nan, stop_frac)
    is_origin = np.where(np.isnan(rank), np.nan, (rank == 0).astype(np.float64))
    is_terminus = np.where(
        np.isnan(rank) | np.isnan(n_stops), np.nan, (rank == n_stops - 1).astype(np.float64)
    )

    out: dict[str, np.ndarray] = {
        "is_holiday": np.asarray(raw["is_holiday"], dtype=np.float32),
        "sched_min": sched_min.astype(np.float32),
        "trip_start_min": start_min.astype(np.float32),
        # NaN start propagates: an unknown start means an unknown elapsed time.
        "minutes_since_start": ((sched_min - start_min) % 1440).astype(np.float32),
        "stop_frac": stop_frac.astype(np.float32),
        "n_stops": n_stops.astype(np.float32),
        "is_origin": is_origin.astype(np.float32),
        "is_terminus": is_terminus.astype(np.float32),
        "lat": np.asarray(raw["lat"], dtype=np.float32),
        "lon": np.asarray(raw["lon"], dtype=np.float32),
    }
    angle = 2 * np.pi * np.asarray(raw["day_of_year_idx"], dtype=np.float64) / DAYS_IN_CYCLE
    out["doy_sin"] = np.sin(angle).astype(np.float32)
    out["doy_cos"] = np.cos(angle).astype(np.float32)
    return out


def build_vocabularies(features: dict[str, np.ndarray]) -> dict[str, list]:
    """Sorted distinct values of each categorical feature, as JSON-able lists."""
    vocab: dict[str, list] = {}
    for name in CATEGORICAL:
        values = np.unique(features[name])
        vocab[name] = [v.item() if hasattr(v, "item") else v for v in values]
    return vocab


def encode_categoricals(features: dict[str, np.ndarray], vocab: dict[str, list]) -> dict[str, np.ndarray]:
    """Replace each categorical column by its int32 code in `vocab`; UNKNOWN if absent."""
    out = dict(features)
    for name in CATEGORICAL:
        out[name] = encode_column(features[name], vocab[name])
    return out


def encode_column(values: np.ndarray, known: list) -> np.ndarray:
    """int32 position of each value in the sorted vocabulary list `known`; UNKNOWN if absent."""
    is_text = values.dtype == object
    known = np.array(known, dtype=object if is_text else np.int64)
    if len(known) == 0:
        return np.full(len(values), UNKNOWN, dtype=np.int32)
    if is_text:
        known = known.astype(str)
        values = values.astype(str)
    pos = np.searchsorted(known, values)
    pos_clipped = np.minimum(pos, len(known) - 1)
    hit = known[pos_clipped] == values
    return np.where(hit, pos_clipped, UNKNOWN).astype(np.int32)


def feature_matrix(encoded: dict[str, np.ndarray]) -> np.ndarray:
    """float32 (n, len(FEATURES)) in FEATURES order -- the positional contract."""
    return np.column_stack([encoded[name].astype(np.float32) for name in FEATURES])


def weekday_of(service_date: str | date) -> int:
    day = date.fromisoformat(service_date) if isinstance(service_date, str) else service_date
    return day.weekday()


def day_of_year_index(service_date: str | date) -> int:
    """0-based position of the date's month/day in a leap year; the year itself is ignored."""
    day = date.fromisoformat(service_date) if isinstance(service_date, str) else service_date
    return int(_MONTH_OFFSET[day.month - 1]) + day.day - 1


def day_of_year_indices(epoch_days: np.ndarray) -> np.ndarray:
    """`day_of_year_index` for days since 1970-01-01, vectorised."""
    d = np.asarray(epoch_days, dtype="int64").astype("datetime64[D]")
    month = d.astype("datetime64[M]")
    day = (d - month).astype(np.int64)
    return _MONTH_OFFSET[(month.astype(np.int64) % 12)] + day


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


def corridor_direction(train: Train, same_category_lineless: list[Train]) -> int:
    """0/1 for a line-less train, against the longest route on its stretch.

    The corridor is `direction.corridor_members` around the train's own route --
    the same membership rule the search view uses. Its axis is the longest
    member route (ties: lowest train id), so every train on a stretch is
    measured against the same, deterministic axis no matter which member asks.
    """
    route = [part for part in (train.route_path or "").split("|") if part]
    if len(route) < 2:
        return UNKNOWN_DIRECTION
    members = corridor_members(route, same_category_lineless) or [train]
    axis_train = max(members, key=lambda t: (len((t.route_path or "").split("|")), -t.id))
    axis = [part for part in (axis_train.route_path or "").split("|") if part]
    index = {name: position for position, name in enumerate(axis)}
    return direction_of(train, index)


def all_directions(session: Session) -> dict[int, int]:
    """train_id -> 0/1 for every train (UNKNOWN_DIRECTION where unresolvable)."""
    out: dict[int, int] = {}
    for line_id in session.execute(select(ServiceLine.id)).scalars():
        out.update(direction_by_train_id(session, line_id))

    by_category: dict[str | None, list[Train]] = {}
    for train in session.execute(select(Train).where(Train.line_id.is_(None))).scalars():
        by_category.setdefault(train.category, []).append(train)
    for trains in by_category.values():
        for train in trains:
            out[train.id] = corridor_direction(train, trains)
    return out


def direction_for(session: Session, train: Train) -> int:
    """One train's direction, computed exactly as `all_directions` would."""
    if train.line_id is not None:
        return direction_by_train_id(session, train.line_id).get(train.id, UNKNOWN_DIRECTION)
    same_kind = session.execute(
        select(Train).where(Train.line_id.is_(None), Train.category == train.category)
    ).scalars().all()
    return corridor_direction(train, list(same_kind))
