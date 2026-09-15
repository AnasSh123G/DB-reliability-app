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
    target_mean_s: float


# Calibrated against published German punctuality: long distance is the outlier,
# with roughly three in eight trains over six minutes late.
SEGMENT_PROFILES: dict[str, DelayProfile] = {
    SEGMENT_FV: DelayProfile(
        punctual_rate=0.62,
        shape_k=1.5,
        early_offset_s=30,
        tail_mu=6.37,
        tail_sigma=0.90,
        cancel_rate=0.020,
        target_mean_s=540.0,
    ),
    SEGMENT_RV: DelayProfile(
        punctual_rate=0.90,
        shape_k=3.0,
        early_offset_s=30,
        tail_mu=5.91,
        tail_sigma=1.00,
        cancel_rate=0.015,
        target_mean_s=150.0,
    ),
    SEGMENT_S: DelayProfile(
        punctual_rate=0.93,
        shape_k=4.0,
        early_offset_s=20,
        tail_mu=5.64,
        tail_sigma=1.00,
        cancel_rate=0.010,
        target_mean_s=105.0,
    ),
}
_DEFAULT_PROFILE = SEGMENT_PROFILES[SEGMENT_RV]

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
    service_dates = [
        (first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
    ]

    trip_rows: list[dict] = []
    stop_rows: list[dict] = []

    for train_id, stops in by_train.items():
        profile = _profile_for(segments.get(train_id))
        for service_date in service_dates:
            cancelled_run = rng.random() < profile.cancel_rate

            # One base delay per run, then a random walk along it. Independent
            # per-stop draws would make every station's average identical and the
            # station board pointless — a late train is late for a while.
            base = sample_delay(rng, profile)
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
                delay = base + walk + _station_bias(seed, station_id)
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
                    "n_stops_observed": 0 if cancelled_run else len(built),
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
                    "last_observed_at": now,
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
