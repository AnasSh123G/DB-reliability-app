"""Turning trip_stop rows into something a model can be fitted on.

Two things here are load-bearing and easy to get wrong.

**Only facts that exist before the day runs.** Every feature is declared in
FEATURE_SPEC with the fact it comes from, and a test asserts those facts are
disjoint from LEAKAGE_COLUMNS. The supposed times are read from trip_stop rather
than joined from station_stop because, per TripStop's own docstring, the planned
time of a run that already happened is a historical fact — which is exactly what
makes point-in-time feature building safe here.

**The split is by whole service date, never by row.** A random split would put
the 08:14 stop of a run in training and its 08:31 stop in test, and the model
would score brilliantly by having already seen that run. Splitting on dates, in
time order, is the only arrangement that answers the question the model exists
for: what happens on a day we have not seen. The measured consequence of getting
this wrong is not subtle — grouping this data by train alone reaches R² 0.79
in-sample, and a random split would report that number as skill.

No lightgbm and no numpy in this module: rows come out as plain lists, and only
the trainer turns them into arrays.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.derive._common import sched_sec_of
from dbrel.holidays_de import day_type_of, load_holiday_dates
from dbrel.ml._common import CATEGORICAL_INDEXES, FEATURE_NAMES, FEATURE_SPEC
from dbrel.models import TRUSTED_QUALITIES, Station, Train, Trip, TripStop

# The value a categorical takes when the fitted vocabulary has never seen it —
# a train added after training, a newly tracked station. LightGBM treats it as
# its own category rather than crashing, which is the honest behaviour: we know
# nothing about it, and the model should fall back on everything else it knows.
UNKNOWN_CODE = -1


def build_row(
    *,
    day_type: str,
    train_id: int,
    station_id: int,
    sched_sec: int,
    line_id: int | None,
    category: str | None,
    operator: str | None,
    segment: str | None,
    platform: str | None,
    stop_sequence: int | None,
    n_stops: int | None,
    first_departure_sec: int | None,
    lat: float | None,
    lon: float | None,
) -> list:
    """One feature row, in FEATURE_SPEC order.

    Training and scoring both come through here. They must not each assemble the
    row themselves: a booster indexes features positionally, so the day the two
    drift apart the model keeps predicting — confidently, from the wrong columns,
    with nothing to raise an error. Same reasoning as derive/_common.py.
    """
    return [
        day_type,
        train_id,
        station_id,
        line_id,
        category,
        operator,
        segment,
        platform,
        sched_sec,
        (sched_sec // 3600) % 24,
        (sched_sec % 3600) // 60,
        stop_sequence,
        (stop_sequence / n_stops) if (stop_sequence and n_stops) else 0.5,
        n_stops,
        first_departure_sec,
        lat,
        lon,
    ]


@dataclass(slots=True)
class Dataset:
    """Raw feature rows plus everything needed to split and score them.

    `rows` holds categoricals as their raw values (strings, ids, None); encoding
    to integer codes happens after the split, because a vocabulary built over
    the test period would be a small but real leak.
    """

    rows: list[list] = field(default_factory=list)
    labels: list[float] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    keys: list[tuple] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)


@dataclass(slots=True)
class Split:
    """Row indexes for each period, and the dates that produced them."""

    fit: list[int]
    calibrate: list[int]
    test: list[int]
    fit_dates: list[str]
    calibrate_dates: list[str]
    test_dates: list[str]


def extract(
    session: Session,
    *,
    since: str | None = None,
    until: str | None = None,
    progress=lambda msg: None,
) -> Dataset:
    """Every usable observation in the window, as feature rows and labels.

    Usable means RECORDED with a departure delay and is_handover = 0 — the same
    two honesty filters the aggregates apply, for the same reasons: NO_DATA is an
    unknown rather than a zero, and a handover stop would count one physical call
    twice.
    """
    holiday_dates = load_holiday_dates(session)

    query = (
        select(
            Trip.service_date,
            Trip.train_id,
            TripStop.station_id,
            TripStop.stop_sequence,
            TripStop.supposed_arrival_utc,
            TripStop.supposed_departure_utc,
            TripStop.dep_delay_s,
            TripStop.planned_platform,
            Train.category,
            Train.operator,
            Train.segment,
            Train.line_id,
            Train.n_stops,
            Train.first_departure_sec,
            Station.lat,
            Station.lon,
        )
        .join(Trip, Trip.id == TripStop.trip_id)
        .join(Train, Train.id == Trip.train_id)
        .join(Station, Station.id == TripStop.station_id)
        .where(
            TripStop.is_handover == 0,
            TripStop.quality.in_(TRUSTED_QUALITIES),
            TripStop.dep_delay_s.isnot(None),
        )
    )
    if since:
        query = query.where(Trip.service_date >= since)
    if until:
        query = query.where(Trip.service_date <= until)

    data = Dataset()
    for (
        service_date, train_id, station_id, stop_sequence, sup_arr, sup_dep,
        dep_delay, platform, category, operator, segment, line_id, n_stops,
        first_departure_sec, lat, lon,
    ) in session.execute(query.execution_options(yield_per=10_000)):
        day_type = day_type_of(service_date, holiday_dates)
        if day_type is None:
            continue
        scheduled = sched_sec_of(service_date, sup_arr, sup_dep)
        if scheduled is None:
            continue
        sched_sec = scheduled[0]

        data.rows.append(build_row(
            day_type=day_type, train_id=train_id, station_id=station_id,
            sched_sec=sched_sec, line_id=line_id, category=category,
            operator=operator, segment=segment, platform=platform,
            stop_sequence=stop_sequence, n_stops=n_stops,
            first_departure_sec=first_departure_sec, lat=lat, lon=lon,
        ))
        data.labels.append(float(dep_delay))
        data.dates.append(service_date)
        data.keys.append((day_type, train_id, station_id, sched_sec))

    progress(f"  {len(data):,} observations over {len(set(data.dates))} service dates")
    return data


def split_by_date(dates: list[str], *, fit_share: float = 0.6, cal_share: float = 0.2) -> Split:
    """Whole dates, oldest first: fit, then calibrate, then test.

    Whole dates rather than whole trips because a trip cannot straddle midnight
    into another service date by definition, so dates are already the coarsest
    unit that keeps a run intact.
    """
    distinct = sorted(set(dates))
    n = len(distinct)
    n_fit = max(1, int(n * fit_share))
    n_cal = max(1, int(n * cal_share)) if n - n_fit > 1 else 0
    fit_dates = set(distinct[:n_fit])
    cal_dates = set(distinct[n_fit:n_fit + n_cal])
    test_dates = set(distinct[n_fit + n_cal:])

    split = Split([], [], [], sorted(fit_dates), sorted(cal_dates), sorted(test_dates))
    for index, date in enumerate(dates):
        if date in fit_dates:
            split.fit.append(index)
        elif date in cal_dates:
            split.calibrate.append(index)
        else:
            split.test.append(index)
    return split


def build_vocabularies(data: Dataset, indexes: list[int]) -> dict[str, dict[str, int]]:
    """Categorical value -> integer code, learned from the fit split only.

    Built on `indexes` rather than the whole dataset so that a category which
    only ever appears after the split boundary stays genuinely unseen. Codes are
    assigned in sorted order so the same data always produces the same model.
    """
    vocabularies: dict[str, dict[str, int]] = {}
    for position in CATEGORICAL_INDEXES:
        name = FEATURE_NAMES[position]
        values = {data.rows[i][position] for i in indexes}
        values.discard(None)
        vocabularies[name] = {
            str(value): code for code, value in enumerate(sorted(values, key=str))
        }
    return vocabularies


def encode(data: Dataset, vocabularies: dict[str, dict[str, int]]) -> list[list[float]]:
    """Raw rows to numeric rows, categoricals through the fitted vocabulary."""
    categorical_positions = set(CATEGORICAL_INDEXES)
    encoded = []
    for row in data.rows:
        out = []
        for position, value in enumerate(row):
            if position in categorical_positions:
                vocabulary = vocabularies[FEATURE_NAMES[position]]
                out.append(float(vocabulary.get(str(value), UNKNOWN_CODE)))
            else:
                out.append(float(value) if value is not None else 0.0)
        encoded.append(out)
    return encoded


def feature_names() -> list[str]:
    return [f.name for f in FEATURE_SPEC]
