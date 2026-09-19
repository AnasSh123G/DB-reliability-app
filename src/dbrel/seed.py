"""Seeded demonstration runs, so the app has something to show before real
observations accumulate.

Two rules keep this honest:

* **It writes trip and trip_stop, never train_station_delay.** The seeder produces
  the same shape of row the poller produces, and the same averaging pass consumes
  it. Writing averages directly would let a bug in the averaging logic hide behind
  data that was tailored to it.
* **Synthetic runs are tagged trip.source='synth' and live on dates strictly
  before the earliest real run.** uq_trip_train_date is UNIQUE(train_id,
  service_date), so a synthetic run sharing a date with a real one would collide;
  separating the ranges also means `--clear` can never take a real row with it.

Only the actuals are invented. What runs, where it calls and at what planned time
all come from the real station_stop pattern, so a seeded board shows real trains at
real times — just with delays that never happened.

The day of the week is a first-class dimension, because that is what the `dow`
column on train_station_delay and the seven day_* views exist to separate. Two
tables shape it: DOW_DELAY_SCALE moves the delay level, and DOW_SERVICE_RATE
thins the timetable at weekends, so a Sunday differs from a Tuesday in what runs
as well as in how late it is.
"""

from __future__ import annotations

import random
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.config import PUNCTUAL_S, RULE_VERSION
from dbrel.gtfs.timeutil import local_hour
from dbrel.identity import SEGMENT_FV, SEGMENT_RV, SEGMENT_S, SOURCE_SYNTH
from dbrel.models import (
    BASIS_SYNTH,
    QUALITY_CANCELLED,
    QUALITY_NO_DATA,
    QUALITY_RECORDED,
    StationStop,
    Train,
    TrainStationDelay,
    Trip,
    TripStop,
)
from dbrel.timetables.runs import epoch_of_sec, tt_feed_version


@dataclass(frozen=True, slots=True)
class DelayProfile:
    """The marginal delay distribution for one segment.

    punctual_rate and target_mean_s are the two published figures, and they are
    not independently satisfiable: they constrain each other through

        mean = p·(PUNCTUAL_S/(k+1) − early) + (1−p)·(PUNCTUAL_S + L)

    where L = exp(mu + sigma²/2) is the mean of the lognormal tail measured above
    the punctuality threshold. k therefore has to be solved per segment, not
    picked: at S-Bahn's p=0.93 and mean=105 s a naive k=1 already spends the whole
    budget on the punctual block and forces L negative.

    tests/test_seed.py draws from each profile and asserts the realised figures,
    which is what turns these constants from assertion into contract.
    """

    punctual_rate: float
    shape_k: float
    early_offset_s: int
    tail_mu: float
    tail_sigma: float
    cancel_rate: float
    no_data_rate: float
    target_mean_s: float


# Fitted to the one complete day this project has actually measured: Friday
# 2026-09-18 over the 212 tracked Ruhr stations, quality='RECORDED', is_handover=0
# (24,580 stops, mean departure delay 333.5 s). Deliberately not the published
# national figures, which are far more optimistic than this sample on every
# segment — RV alone measured 0.68 punctual against a published 0.90.
#
# How each number was obtained, so it can be refitted when more days exist:
#   punctual_rate  share of stops with dep_delay_s < PUNCTUAL_S
#   tail_mu        ln(median of dep_delay_s - PUNCTUAL_S over the tail)
#   tail_sigma     sqrt(2 * ln(tail mean / tail median))
#   shape_k        solved from mean = p*(PUNCTUAL_S/(k+1) - early) + (1-p)*(PUNCTUAL_S + L)
#   cancel_rate    share of the segment's stops graded CANCELLED
#   no_data_rate   share graded NO_DATA
# early_offset_s is 0 on every segment: the measured share of early departures is
# between 0.0% and 0.3%, so the 20-30 s offset the published profiles carried was
# inventing early trains that this railway does not run.
#
# tests/test_seed.py draws from each profile and asserts the realised figures,
# which is what turns these constants from assertion into contract.
SEGMENT_PROFILES: dict[str, DelayProfile] = {
    SEGMENT_FV: DelayProfile(
        punctual_rate=0.3441,
        # The one segment where the three measured figures are not jointly
        # satisfiable: at the measured tail mean the tail alone already accounts
        # for the whole mean, leaving the punctual block nothing to contribute.
        # tail_sigma is therefore fitted to the tail *median* and then trimmed
        # (0.764 -> 0.662) until k lands in a sane range, which costs a little of
        # the most extreme tail and keeps the mean and punctuality exact.
        shape_k=1.5,
        early_offset_s=0,
        tail_mu=6.867,
        tail_sigma=0.662,
        cancel_rate=0.041,
        no_data_rate=0.031,
        target_mean_s=1069.2,
    ),
    SEGMENT_RV: DelayProfile(
        punctual_rate=0.6827,
        shape_k=5.58,
        early_offset_s=0,
        tail_mu=6.040,
        tail_sigma=0.985,
        cancel_rate=0.020,
        no_data_rate=0.055,
        target_mean_s=368.1,
    ),
    SEGMENT_S: DelayProfile(
        punctual_rate=0.7905,
        shape_k=4.19,
        early_offset_s=0,
        tail_mu=5.704,
        tail_sigma=0.974,
        cancel_rate=0.022,
        no_data_rate=0.011,
        target_mean_s=231.2,
    ),
}
_DEFAULT_PROFILE = SEGMENT_PROFILES[SEGMENT_RV]

# Indexed by Python's weekday(): 0=Monday..6=Sunday, the same convention as the
# dow column and src/dbrel/views.py. Friday is 1.0 by construction — the profiles
# above are fitted to a real Friday, so every other day is expressed relative to
# it and a synthetic Friday stays directly comparable to the measured one.
DOW_DELAY_SCALE = (1.02, 0.94, 0.95, 1.01, 1.00, 0.78, 0.72)

# Share of the weekday timetable that runs at all. Which trains are dropped is
# decided per (train, weekday) and not per date, so the same services are missing
# every Saturday — a fresh random subset each week is not a timetable, and it
# would smear the weekday signal these views exist to show.
DOW_SERVICE_RATE = (1.0, 1.0, 1.0, 1.0, 1.0, 0.70, 0.55)

# A train never arrives more than three minutes early in practice, and a delay
# beyond four hours is a different kind of event than the one being modelled.
_MIN_DELAY_S = -180
_MAX_DELAY_S = 4 * 3600


@dataclass(slots=True)
class SeedResult:
    trips_written: int = 0
    stops_written: int = 0
    trips_deleted: int = 0
    stops_deleted: int = 0
    averages_deleted: int = 0
    first_date: str | None = None
    last_date: str | None = None
    elapsed_s: float = 0.0


def sample_delay(rng: random.Random, profile: DelayProfile) -> int:
    """One marginal delay draw, in seconds.

    The punctual block is PUNCTUAL_S · U^k, which concentrates near zero as k
    grows — an S-Bahn that is 'on time' is usually within a minute, not uniformly
    scattered across six. The tail is lognormal measured above the threshold, so
    it is right-skewed with no upper bound short of the clamp.
    """
    if rng.random() < profile.punctual_rate:
        return int(PUNCTUAL_S * rng.random() ** profile.shape_k) - profile.early_offset_s
    return PUNCTUAL_S + int(rng.lognormvariate(profile.tail_mu, profile.tail_sigma))


def _station_bias(seed: int, station_id: int) -> int:
    """A fixed per-station offset, so hubs read worse than branch halts.

    Derived from the station id rather than drawn per run: the whole point of the
    board is that the average differs *by station*, and independent draws would
    make every station converge on the same number. Mean-zero, so it spreads the
    stations apart without moving the segment's overall figure.
    """
    return int(max(-90, min(120, random.Random(seed ^ (station_id * 2_654_435_761)).gauss(0, 45))))


def _train_bias(seed: int, train_id: int) -> int:
    """A fixed per-train offset, so some services are reliably worse than others.

    _station_bias spreads the stations apart; without this, every train calling at
    a given station would converge on that station's number and a per-train
    average would carry no information. Mean-zero for the same reason, and drawn
    from the train id so one train keeps its character across the whole month.
    """
    return int(max(-150, min(150, random.Random(seed ^ (train_id * 2_246_822_519)).gauss(0, 60))))


def _raw_date_shock(seed: int, service_date: str) -> float:
    """One offset shared by every run on a given date, before centring.

    Stands in for that day's network-wide conditions — weather, a signal failure,
    an engineering possession. It is what makes the four synthetic Fridays differ
    from each other rather than being four draws from one distribution.
    """
    # int(YYYYMMDD), not hash(): str.__hash__ is salted per process unless
    # PYTHONHASHSEED is set, and "same seed, same data" is a documented promise.
    key = int(service_date.replace("-", "")) * 2_654_435_761
    return max(-120.0, min(180.0, random.Random(seed ^ key).gauss(0, 45)))


def date_shocks(seed: int, service_dates: list[str]) -> dict[str, int]:
    """Per-date shocks, centred so each weekday's own shocks sum to zero.

    Centring is not cosmetic. A month holds only four of each weekday, so the mean
    of four draws at sigma=45 has a standard error of ~22 s — comparable to the
    gaps in DOW_DELAY_SCALE itself. Uncentred, the noise routinely outranks the
    signal: on the first real 28-day run the four Sundays happened to average
    +51 s and the four Saturdays -11.5 s, which flipped Sunday above Saturday and
    inverted the weekday ordering the seven views exist to show.

    Subtracting each weekday's own mean keeps every date different from its
    neighbours while making the weekday effect exactly what DOW_DELAY_SCALE says
    it is. Both properties were asked for; this is what lets them coexist.
    """
    by_dow: dict[int, list[str]] = defaultdict(list)
    for service_date in service_dates:
        by_dow[date_cls.fromisoformat(service_date).weekday()].append(service_date)

    shocks: dict[str, int] = {}
    for dates in by_dow.values():
        raw = {d: _raw_date_shock(seed, d) for d in dates}
        mean = sum(raw.values()) / len(raw)
        for service_date, value in raw.items():
            shocks[service_date] = int(round(value - mean))
    return shocks


def _runs_on(seed: int, train_id: int, dow: int) -> bool:
    """Whether this train is part of the timetable on this weekday at all.

    Deterministic in (train_id, dow), so a service absent one Saturday is absent
    every Saturday. See DOW_SERVICE_RATE.
    """
    draw = random.Random(seed ^ (train_id * 40_503) ^ (dow * 2_654_435_761)).random()
    return draw < DOW_SERVICE_RATE[dow]


def _profile_for(segment: str | None) -> DelayProfile:
    return SEGMENT_PROFILES.get(segment or "", _DEFAULT_PROFILE)


def _synthetic_window(
    session: Session, days: int, end_date: str | None
) -> tuple[date_cls, date_cls] | None:
    """The date range to seed: `days` long, ending strictly before any real run."""
    today = date_cls.today()
    last = date_cls.fromisoformat(end_date) if end_date else today - timedelta(days=1)

    first_real = session.scalar(
        select(func.min(Trip.service_date)).where(Trip.source != SOURCE_SYNTH)
    )
    if first_real:
        last = min(last, date_cls.fromisoformat(first_real) - timedelta(days=1))

    first = last - timedelta(days=days - 1)
    if first > last:
        return None
    return first, last


def seed_synthetic(
    session: Session,
    *,
    days: int = 28,
    end_date: str | None = None,
    station_ids: list[int] | None = None,
    seed: int = 20260914,
    progress=lambda msg: None,
) -> SeedResult:
    started = time.monotonic()
    result = SeedResult()
    rng = random.Random(seed)

    window = _synthetic_window(session, days, end_date)
    if window is None:
        progress("  no room before the earliest real run; nothing seeded")
        return result
    first, last = window
    result.first_date, result.last_date = first.isoformat(), last.isoformat()
    progress(f"  seeding {result.first_date} .. {result.last_date}")

    fv_id = tt_feed_version(session)

    # The real pattern is the script: which trains call where, in what order, at
    # what planned second. Only tracked stations have station_stop rows at all.
    pattern_query = (
        select(
            StationStop.train_id,
            StationStop.stop_sequence,
            StationStop.station_id,
            StationStop.id,
            StationStop.platform_code,
            StationStop.planned_arrival_sec,
            StationStop.planned_departure_sec,
            Train.segment,
        )
        .join(Train, Train.id == StationStop.train_id)
        .where(StationStop.feed_version_id == fv_id)
        .order_by(StationStop.train_id, StationStop.stop_sequence)
    )
    if station_ids:
        pattern_query = pattern_query.where(StationStop.station_id.in_(station_ids))

    by_train: dict[int, list[tuple]] = defaultdict(list)
    segments: dict[int, str | None] = {}
    for train_id, seq, station_id, ss_id, platform, arr_sec, dep_sec, segment in session.execute(
        pattern_query
    ):
        by_train[train_id].append((seq, station_id, ss_id, platform, arr_sec, dep_sec))
        segments[train_id] = segment

    if not by_train:
        progress("  no station_stop pattern found; run tt-import-plan first")
        return result

    now = int(time.time())
    # (date, weekday, that day's shared shock) — the weekday and the shock are
    # properties of the date, so they are resolved once rather than per train.
    days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    shocks = date_shocks(seed, [day.isoformat() for day in days])
    calendar = [(day.isoformat(), day.weekday(), shocks[day.isoformat()]) for day in days]

    trip_rows: list[dict] = []
    stop_rows: list[dict] = []

    for train_id, stops in by_train.items():
        profile = _profile_for(segments.get(train_id))
        train_bias = _train_bias(seed, train_id)
        for service_date, dow, date_shock in calendar:
            if not _runs_on(seed, train_id, dow):
                continue
            cancelled_run = rng.random() < profile.cancel_rate

            # One base delay per run, then a random walk along it. Independent
            # per-stop draws would make every station's average identical and the
            # station board pointless — a late train is late for a while.
            base = int(sample_delay(rng, profile) * DOW_DELAY_SCALE[dow])
            walk = 0

            built: list[dict] = []
            for seq, station_id, ss_id, platform, arr_sec, dep_sec in stops:
                supposed_arr = epoch_of_sec(arr_sec, service_date)
                supposed_dep = epoch_of_sec(dep_sec, service_date)
                if supposed_arr is None and supposed_dep is None:
                    continue

                if cancelled_run:
                    built.append(
                        {
                            "stop_sequence": seq,
                            "station_id": station_id,
                            "station_stop_id": ss_id,
                            "supposed_arrival_utc": supposed_arr,
                            "supposed_departure_utc": supposed_dep,
                            "actual_arrival_utc": None,
                            "actual_departure_utc": None,
                            "arr_delay_s": None,
                            "dep_delay_s": None,
                            "quality": QUALITY_CANCELLED,
                            "cancelled": 1,
                            "arr_cancelled": 1 if supposed_arr else 0,
                            "dep_cancelled": 1 if supposed_dep else 0,
                            "cancelled_at_utc": (supposed_dep or supposed_arr) - 3600,
                            "planned_platform": platform,
                        }
                    )
                    continue

                # Mean-zero, and never floored against `base`. A drifting walk
                # would push the realised mean above the segment's published
                # figure, and clamping it at -base truncates exactly the negative
                # excursions — biasing hardest for S-Bahn, where base is smallest.
                # The clamp below is the only floor, and it is wide enough to bind
                # rarely.
                walk += int(rng.gauss(0, 70))

                # A stop nobody managed to read. Drawn per stop rather than per
                # run because that is what a real gap is — the train ran, one
                # value went unobserved — and the walk above is advanced first so
                # the gap does not alter what the following stops look like.
                if rng.random() < profile.no_data_rate:
                    built.append(
                        {
                            "stop_sequence": seq,
                            "station_id": station_id,
                            "station_stop_id": ss_id,
                            "supposed_arrival_utc": supposed_arr,
                            "supposed_departure_utc": supposed_dep,
                            "actual_arrival_utc": None,
                            "actual_departure_utc": None,
                            "arr_delay_s": None,
                            "dep_delay_s": None,
                            "quality": QUALITY_NO_DATA,
                            "cancelled": 0,
                            "arr_cancelled": 0,
                            "dep_cancelled": 0,
                            "cancelled_at_utc": None,
                            "planned_platform": platform,
                        }
                    )
                    continue

                delay = base + walk + _station_bias(seed, station_id) + train_bias + date_shock
                dep_delay = max(_MIN_DELAY_S, min(_MAX_DELAY_S, delay))
                # Arriving slightly later than the departure delay: the train makes
                # up a little of it standing at the platform.
                arr_delay = max(_MIN_DELAY_S, dep_delay + int(rng.random() * 60))

                built.append(
                    {
                        "stop_sequence": seq,
                        "station_id": station_id,
                        "station_stop_id": ss_id,
                        "supposed_arrival_utc": supposed_arr,
                        "supposed_departure_utc": supposed_dep,
                        "actual_arrival_utc": supposed_arr + arr_delay if supposed_arr else None,
                        "actual_departure_utc": supposed_dep + dep_delay if supposed_dep else None,
                        "arr_delay_s": arr_delay if supposed_arr else None,
                        "dep_delay_s": dep_delay if supposed_dep else None,
                        "quality": QUALITY_RECORDED,
                        "cancelled": 0,
                        "arr_cancelled": 0,
                        "dep_cancelled": 0,
                        "cancelled_at_utc": None,
                        "planned_platform": platform,
                    }
                )

            if not built:
                continue

            first_stop, last_stop = built[0], built[-1]
            delays = [
                row["dep_delay_s"] if row["dep_delay_s"] is not None else row["arr_delay_s"]
                for row in built
            ]
            delays = [d for d in delays if d is not None]
            run_key = f"synth:{train_id}:{service_date}"

            trip_rows.append(
                {
                    "train_id": train_id,
                    "service_date": service_date,
                    "source": SOURCE_SYNTH,
                    "source_run_key": run_key,
                    "feed_version_id": fv_id,
                    "status": "CANCELLED" if cancelled_run else "COMPLETED",
                    "supposed_departure_utc": first_stop["supposed_departure_utc"],
                    "supposed_arrival_utc": last_stop["supposed_arrival_utc"],
                    "actual_departure_utc": first_stop["actual_departure_utc"],
                    "actual_arrival_utc": last_stop["actual_arrival_utc"],
                    "cancelled_at_utc": first_stop["cancelled_at_utc"],
                    "final_delay_s": (
                        last_stop["arr_delay_s"]
                        if last_stop["arr_delay_s"] is not None
                        else last_stop["dep_delay_s"]
                    ),
                    "max_delay_s": max(delays) if delays else None,
                    "n_stops": len(built),
                    "n_stops_tracked": len(built),
                    "n_stops_observed": sum(
                        1 for row in built if row["quality"] == QUALITY_RECORDED
                    ),
                    "quality": QUALITY_CANCELLED if cancelled_run else QUALITY_RECORDED,
                    "rule_version": RULE_VERSION,
                    # Stamped so the averages watermark treats these as settled;
                    # nothing will ever poll them.
                    "finalized_at": now,
                }
            )
            stop_rows.append({"_run_key": run_key, "_service_date": service_date, "rows": built})

    if not trip_rows:
        progress("  nothing to seed")
        result.elapsed_s = time.monotonic() - started
        return result

    # DO NOTHING with no conflict target, so a re-run is a no-op rather than a
    # duplicate. trip carries two unique constraints — (train_id, service_date) as
    # well as the run key — and naming only one of them means the other raises
    # instead of being ignored.
    for chunk in range(0, len(trip_rows), 5000):
        session.execute(
            sqlite_insert(Trip).on_conflict_do_nothing(), trip_rows[chunk : chunk + 5000]
        )
    session.flush()
    result.trips_written = len(trip_rows)

    trip_ids = {
        (run_key, service_date): trip_id
        for trip_id, run_key, service_date in session.execute(
            select(Trip.id, Trip.source_run_key, Trip.service_date).where(
                Trip.source == SOURCE_SYNTH
            )
        )
    }

    flat: list[dict] = []
    for bundle in stop_rows:
        trip_id = trip_ids.get((bundle["_run_key"], bundle["_service_date"]))
        if trip_id is None:
            continue
        for row in bundle["rows"]:
            departure = row["supposed_departure_utc"]
            flat.append(
                {
                    **row,
                    "trip_id": trip_id,
                    "rule_version": RULE_VERSION,
                    "is_handover": 0,
                    "added": 0,
                    "changed_platform": None,
                    # A gap row is by definition one nothing was ever read for, the
                    # same shape derive/finalize_tt.py writes. It also keeps such
                    # rows out of the incremental averaging pass, which watermarks
                    # on last_observed_at.
                    "last_observed_at": None if row["quality"] == QUALITY_NO_DATA else now,
                    "read_lag_s": int(rng.random() * 3300) + 300,
                    "supposed_departure_hour": local_hour(departure) if departure else None,
                }
            )

    for chunk in range(0, len(flat), 5000):
        session.execute(
            sqlite_insert(TripStop).on_conflict_do_nothing(
                index_elements=["trip_id", "stop_sequence"]
            ),
            flat[chunk : chunk + 5000],
        )
    session.flush()
    result.stops_written = len(flat)
    result.elapsed_s = time.monotonic() - started
    return result


def clear_synthetic(session: Session) -> SeedResult:
    """Remove every seeded row, and only seeded rows."""
    started = time.monotonic()
    result = SeedResult()

    synthetic = select(Trip.id).where(Trip.source == SOURCE_SYNTH)
    result.stops_deleted = session.execute(
        delete(TripStop).where(TripStop.trip_id.in_(synthetic))
    ).rowcount
    result.trips_deleted = session.execute(
        delete(Trip).where(Trip.source == SOURCE_SYNTH)
    ).rowcount
    result.averages_deleted = session.execute(
        delete(TrainStationDelay).where(TrainStationDelay.basis == BASIS_SYNTH)
    ).rowcount

    result.elapsed_s = time.monotonic() - started
    return result
