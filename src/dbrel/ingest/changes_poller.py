"""Poll /fchg for the tracked stations and record what has already happened.

This is an archival reader, not a live monitor. /fchg returns its whole horizon on
every poll — forecasts for trains still to come, alongside recorded values for
trains already gone — and only the second kind is wanted here. IRIS never labels
which is which (`ct` is an estimate before passage and DB's recorded actual
afterwards), so the distinction is drawn from the clock: an event whose current
time is far enough in the past has happened, and its `ct` is therefore a fact.

That filter is judged on `ct`, never `pt`. A train booked for 10:00 and running
30 minutes late has not departed at 10:15, and grading it against its planned
time would invent a departure that never occurred.

Recorded actual times go into `trip_stop`. Dated planned-time revisions remain
in an append-only evidence table; later conflicting evidence invalidates a
derived delay while retaining the observed actual time. `finalize` also accounts
for stops that produced no observed value.

One tt_poll row is written per station per sweep, including failures. A station
timing out is data about our own coverage, and must not abort the sweep.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dbrel.ingest.replay import ReplayDayContext

from sqlalchemy import case, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from dbrel.identity import SOURCE_TT
from dbrel.config import GRACE_S
from dbrel.ingest.timeutil import local_date, local_hour, read_clock
from dbrel.models import (
    QUALITY_CANCELLED,
    QUALITY_RECORDED,
    Station,
    Train,
    Trip,
    TripStop,
    TtPoll,
    TtUnmatched,
)
from dbrel.ingest.client import TimetablesClient
from dbrel.ingest.actual_time import ambiguous_actual
from dbrel.ingest.parser import TtEvent, TtStop, parse_timetable
from dbrel.ingest.dated_schedule import (
    DatedPlanned as _Planned, PlanReceipt, record_plans,
    load_patterns as load_dated_patterns,
)
from dbrel.ingest.schedule_schema import require_dated_schedule_schema
from dbrel.ingest.runs import (
    RunContext,
    RunCounters,
    tt_feed_version,
    upsert_run,
)

# rule_version 3: exact dated schedule evidence, without shared TT time fallback.
RULE_VERSION_TT = 3

# IRIS returns DELFI bus and tram halts alongside rail. This project is rail only.
NON_RAIL_CATEGORIES = frozenset({"Bus", "STR", "STB", "U"})

# The published value of a stop, in ONE declared order used both to read the
# existing row and to hash the new one. Comparing hand-written tuples is what this
# replaces: the row written, the row read back and the value compared had drifted
# into three different column orders, which leaked 34% duplicate writes.
VALUE_FIELDS = (
    "supposed_arrival_utc",
    "supposed_departure_utc",
    "station_id",
    "station_stop_id",
    "planned_platform",
    "actual_arrival_utc",
    "actual_departure_utc",
    "arr_delay_s",
    "dep_delay_s",
    "quality",
    "changed_platform",
    "arr_cancelled",
    "dep_cancelled",
    "cancelled_at_utc",
)


@dataclass(slots=True)
class PollResult:
    upsert_calls: int = 0
    refs_refreshed: int = 0
    sweep_id: int = 0
    stations_polled: int = 0
    stations_failed: int = 0
    stops_seen: int = 0
    matched: int = 0
    unmatched: int = 0
    created: int = 0
    pending: int = 0
    stored: int = 0
    messages: int = 0
    cancellations: int = 0
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _TripRef:
    """What resolving a run key yields: enough to place its stops in the schedule."""

    trip_id: int
    train_id: int
    service_date: str
    # Which station_stop variant this trip's own stop_sequence numbers refer
    # to (see StationStop.path_sha1 / runs.py upsert_run). Needed so the
    # pattern lookup below never mixes two route variants of one numbered
    # train under one stop_sequence.
    path_sha1: str | None = None


def _candidate_service_dates(now: int) -> list[str]:
    """A run departing 23:50 is still going after midnight, so both days are live."""
    today = local_date(now)
    return [(today + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)]


def _load_trip_index(session: Session, service_dates: list[str]) -> dict[str, _TripRef]:
    """run_key -> the run. The run key already encodes the date, so it is unique."""
    rows = session.execute(
        select(
            Trip.source_run_key, Trip.id, Trip.train_id, Trip.service_date, Trip.path_sha1
        ).where(Trip.service_date.in_(service_dates), Trip.source == SOURCE_TT)
    ).all()
    return {
        run_key: _TripRef(
            trip_id=trip_id, train_id=train_id, service_date=service_date, path_sha1=path_sha1
        )
        for run_key, trip_id, train_id, service_date, path_sha1 in rows
    }


def _is_full_record(stop: TtStop) -> bool:
    """Whether /fchg sent this stop complete enough to build a run from.

    For an ordinary planned run /fchg is a pure delta — ct with no pt — and the
    planned side has to come from /plan. But additions and replacements, which
    /plan structurally never contains, arrive with <tl> and pt together (they
    co-occur exactly in the samples). Those are the runs worth creating here,
    and the only ones that can be.
    """
    if not stop.category or not stop.number:
        return False
    if stop.category in NON_RAIL_CATEGORIES:
        return False
    return any(
        side is not None and side.planned_ts is not None
        for side in (stop.arrival, stop.departure)
    )


def _has_happened(event: TtEvent | None, now: int) -> bool:
    """Whether this arrival or departure is already in the past.

    The reference is the current published time, falling back to the planned one
    when IRIS has said nothing — never the other way round.
    """
    if event is None:
        return False
    if event.is_cancelled:
        # An explicit cancellation is known when received even if the delta
        # omits both scheduled and actual time. It never creates an actual.
        return event.cancelled_at is None or event.cancelled_at <= now
    if event.changed_ts is not None and ambiguous_actual(event.changed_ts):
        # The repeated autumn hour carries no offset in IRIS. Do not certify
        # the parser's default fold or mistake a second-fold forecast for past.
        return False
    reference = event.changed_ts or event.planned_ts
    if reference is None:
        return False
    return reference + GRACE_S < now


def _resolve(
    time_utc: int | None, delay_s: int | None, supposed: int | None
) -> tuple[int | None, int | None]:
    """Absolute instant and delay for one event, from whichever fields were sent.

    The spec gives the absolute `time` precedence over `delay`; in this feed both are
    always populated together, so either path yields the same answer.
    """
    if supposed is None and time_utc is None:
        return None, None
    if time_utc is not None:
        return time_utc, (time_utc - supposed) if supposed is not None else None
    if delay_s is not None and supposed is not None:
        return supposed + delay_s, delay_s
    return None, delay_s


def _load_patterns(
    session: Session, train_ids: list[int], dates_by_train: dict[int, dict[str, str | None]],
    *, as_of: int | None = None,
) -> dict[tuple[int, str], dict[int, _Planned]]:
    """Use dated TT evidence; retain exact-feed GTFS compatibility."""
    return load_dated_patterns(session, train_ids, dates_by_train, as_of=as_of)


def _existing_values(session: Session, trip_ids: list[int]) -> dict[tuple[int, int], tuple]:
    """(trip, stop) -> the value already recorded, in VALUE_FIELDS order.

    Read through the same field list the new row is built from, so the two can
    never fall out of step.
    """
    known: dict[tuple[int, int], tuple] = {}
    columns = [getattr(TripStop, name) for name in VALUE_FIELDS]
    chunk = 900
    for start in range(0, len(trip_ids), chunk):
        rows = session.execute(
            select(TripStop.trip_id, TripStop.stop_sequence, *columns).where(
                TripStop.trip_id.in_(trip_ids[start : start + chunk])
            )
        ).all()
        for row in rows:
            known[(row[0], row[1])] = tuple(row[2:])
    return known


def run_tt_poll(
    session: Session,
    client: TimetablesClient,
    *,
    priority_max: int = 3,
    sweep_index: int = 0,
    dry_run: bool = False,
    now: int | None = None,
    progress=lambda msg: None,
    day_ctx: ReplayDayContext | None = None,
) -> PollResult:
    require_dated_schedule_schema(session)
    started = time.monotonic()
    # None means live: every clock read below goes to the wall clock, exactly as
    # before. An explicit value pins the whole sweep to a replayed instant.
    frozen = now
    now = read_clock(frozen)
    result = PollResult(sweep_id=now)

    if day_ctx is not None:
        tracked = sorted(day_ctx.tracked_stations, key=lambda st: (st.poll_priority, st.id))
    else:
        tracked = list(session.scalars(
            select(Station)
            .where(Station.tracked == 1, Station.eva.isnot(None))
            .order_by(Station.poll_priority, Station.id)
        ))
    # Priority tiering: a station with priority N is polled every Nth sweep, so a
    # large set can keep high resolution where it matters within the request
    # budget. The actual rate is not a fixed number -- see config.py (TT_RPS_FAST /
    # TT_RPM_PENALTY) and docs/probe-findings.md §27.
    tracked = [
        st
        for st in tracked
        if st.poll_priority <= priority_max and sweep_index % max(1, st.poll_priority) == 0
    ]
    if not tracked:
        result.errors.append("no tracked stations")
        return result

    service_dates = _candidate_service_dates(now)
    trip_by_run = _load_trip_index(session, service_dates)
    run_keys_by_trip: dict[int, set[str]] = {}
    for run_key, ref in trip_by_run.items():
        run_keys_by_trip.setdefault(ref.trip_id, set()).add(run_key)

    def _refresh_refs(trip_id: int) -> None:
        """An alias upsert can repoint the physical trip shared by many keys."""
        trip = session.get(Trip, trip_id)
        current = _TripRef(trip_id, trip.train_id, trip.service_date, trip.path_sha1)
        for run_key in run_keys_by_trip.get(trip_id, ()):
            if trip_by_run[run_key] != current:
                trip_by_run[run_key] = current
                result.refs_refreshed += 1

    # Shared with the /plan importer so both mint identities the same way.
    run_ctx = RunContext(
        fv_id=day_ctx.fv_id if day_ctx is not None else tt_feed_version(session),
        station_id_by_eva={st.eva: st.id for st in tracked},
        station_name_by_eva={st.eva: st.name for st in tracked},
        train_by_key=day_ctx.train_by_key if day_ctx is not None else dict(
            session.execute(
                # Only tts: keys can match; the 87k GTFS trains never will.
                select(Train.identity_key, Train.id).where(Train.source == SOURCE_TT)
            ).all()
        ),
        now=now,
        line_cache=day_ctx.line_cache if day_ctx is not None else {},
    )
    run_counters = RunCounters()

    for station in tracked:
        fetch_started = time.monotonic()
        resp = client.changes(station.eva)
        received_at = resp.recorded_at if resp.recorded_at is not None else read_clock(frozen)
        poll = TtPoll(
            sweep_id=result.sweep_id,
            station_id=station.id,
            eva=station.eva,
            fetched_at=received_at,
            http_status=resp.status,
            bytes=len(resp.body),
            fetch_ms=resp.elapsed_ms,
        )
        if not resp.ok:
            poll.error = resp.body[:200].decode("utf-8", "replace")
            session.add(poll)
            station.n_fchg_errors += 1
            result.stations_failed += 1
            result.errors.append(f"{station.name}: HTTP {resp.status}")
            continue

        try:
            # No keep= predicate: filtering by known run key used to drop unmatched
            # runs inside the parser, before an object existed to count. That hid
            # 80% of returned events behind a counter that could never fire.
            parsed = parse_timetable(resp.body)
        except Exception as exc:
            poll.error = f"parse: {exc}"
            session.add(poll)
            result.stations_failed += 1
            result.errors.append(f"{station.name}: {exc}")
            continue

        poll.content_sha256 = hashlib.sha256(resp.body).hexdigest()
        poll.parse_ms = parsed.parse_ms
        poll.n_stops = parsed.n_raw_stops
        session.add(poll)
        session.flush()

        store_started = time.monotonic()
        observed_at = received_at
        msg_rows: list[dict] = []
        unmatched_rows: list[dict] = []
        # (run, stop, realized arrival, realized departure) for this station.
        realized: list[tuple[_TripRef, TtStop, TtEvent | None, TtEvent | None]] = []
        seen_trip_ids: set[int] = set()
        # Per-station, not cumulative: these land on this station's poll row.
        station_matched = station_unmatched = station_created = 0

        for stop in parsed.stops:
            if stop.eva is None:
                stop = replace(stop, eva=station.eva)
            ref = trip_by_run.get(stop.run_key)
            if _is_full_record(stop):
                # Additions need a trip; known runs need only this stop's receipt.
                receipt = PlanReceipt(observed_at=received_at,
                                      content_sha256=poll.content_sha256,
                                      source_kind="fchg", eva=station.eva)
                was_known = ref is not None
                if ref is not None:
                    # A full station response still describes only one stop.
                    # Keep the established route, endpoints and stop count.
                    if session.get(Trip, ref.trip_id).path_sha1 != ref.path_sha1:
                        _refresh_refs(ref.trip_id)
                        ref = trip_by_run[stop.run_key]
                    record_plans(session, ref.trip_id, ref.path_sha1, [(stop, receipt)],
                                 run_ctx.station_id_by_eva)
                    trip_id = ref.trip_id
                else:
                    trip_id = upsert_run(session, run_ctx, run_counters, stop.run_key, [stop],
                                         schedule_observations=[(stop, receipt)])
                    if trip_id is not None:
                        _refresh_refs(trip_id)
                if trip_id is not None:
                    trip = session.get(Trip, trip_id)
                    ref = _TripRef(
                        trip_id=trip_id,
                        train_id=trip.train_id,
                        service_date=trip.service_date,
                        path_sha1=trip.path_sha1,
                    )
                    trip_by_run[stop.run_key] = ref
                    run_keys_by_trip.setdefault(trip_id, set()).add(stop.run_key)
                    station_created += not was_known
            if ref is None:
                # A delta for a run we have no /plan row for. Recorded rather than
                # dropped: a rising count is the alarm that the roller has fallen
                # behind, and because /plan serves only today and tomorrow that
                # planned data is being lost permanently.
                station_unmatched += 1
                unmatched_rows.append(
                    {
                        "service_date": stop.service_date,
                        "daily_trip_id": stop.daily_trip_id,
                        "stop_index": stop.stop_index,
                        "eva": stop.eva or station.eva,
                        "category": stop.category,
                        "number": stop.number,
                        "first_poll_id": poll.id,
                        "last_poll_id": poll.id,
                    }
                )
                continue
            station_matched += 1
            seen_trip_ids.add(ref.trip_id)

            # Only the side that has actually happened contributes. A stop whose
            # arrival is past but whose departure is still ahead yields its arrival
            # now and its departure on a later poll.
            ar = stop.arrival if _has_happened(stop.arrival, received_at) else None
            dp = stop.departure if _has_happened(stop.departure, received_at) else None
            if ar is None and dp is None:
                result.pending += 1
                continue
            if (ar is None or (ar.changed_ts is None and not ar.is_cancelled)) and (
                dp is None or (dp.changed_ts is None and not dp.is_cancelled)
            ):
                # Past, but IRIS published no time for it — nothing to record.
                continue
            realized.append((ref, stop, ar, dp))

        # The planned side, for exactly the runs this station just yielded.
        # Earlier observations may precede an alias that repointed their trip.
        for trip_id in {ref.trip_id for ref, *_ in realized}:
            _refresh_refs(trip_id)
        realized = [(trip_by_run[stop.run_key], stop, ar, dp)
                    for _, stop, ar, dp in realized]
        dates_by_train: dict[int, dict[str, str | None]] = {}
        for ref, *_ in realized:
            dates_by_train.setdefault(ref.train_id, {})[ref.service_date] = ref.path_sha1
        patterns = _load_patterns(session, list(dates_by_train), dates_by_train, as_of=received_at)

        fact_rows: list[dict] = []
        for ref, stop, ar, dp in realized:
            planned = patterns.get((ref.train_id, ref.service_date), {}).get(stop.stop_index)
            if planned is None:
                # /fchg knows this stop but the schedule does not — the run was
                # created from a delta, or /plan covered a different hour. Without a
                # planned time there is no delay to state, so it is not a fact yet.
                continue
            observed_station_id = run_ctx.station_id_by_eva.get(stop.eva)
            if observed_station_id is not None and observed_station_id != planned.station_id:
                # This stop_sequence's planned pattern belongs to a DIFFERENT
                # station than the one /fchg just reported the observation at.
                # station_stop is written once per (train, index) and assumes
                # that mapping is stable across days (runs.py upsert_run), but
                # a run that sometimes couples with another service shifts its
                # own stops to a different index once coupled -- the same
                # physical stop can then land on an index some OTHER day's
                # pattern already claimed for a different station. Trusting
                # planned here would grade the observation against the wrong
                # stop's scheduled time (e.g. an apparent >10-minute-early
                # arrival that is really a different, correctly-on-time stop).
                # Record it as unmatched instead of writing a corrupted fact.
                station_matched -= 1
                station_unmatched += 1
                unmatched_rows.append(
                    {
                        "service_date": ref.service_date,
                        "daily_trip_id": stop.daily_trip_id,
                        "stop_index": stop.stop_index,
                        "eva": stop.eva,
                        "category": stop.category,
                        "number": stop.number,
                        "first_poll_id": poll.id,
                        "last_poll_id": poll.id,
                    }
                )
                continue
            row = _fact_row(ref, stop, ar, dp, planned, observed_at)
            fact_rows.append(row)
            if row["cancelled"]:
                result.cancellations += 1

            for message in stop.messages:
                msg_rows.append(
                    {
                        "trip_id": ref.trip_id,
                        "stop_sequence": stop.stop_index,
                        "msg_id": message.msg_id,
                        "msg_type": message.msg_type,
                        "code": message.code,
                        "category": message.category,
                        "ts": message.ts,
                        "valid_from": message.valid_from,
                        "valid_to": message.valid_to,
                        "priority": message.priority,
                        "deleted": int(message.deleted),
                    }
                )

        # An already-recorded value reappears on every poll for as long as it
        # lingers in the horizon — roughly 15 hours — so most of fact_rows is
        # usually already stored. Comparing against what exists gives an honest
        # stored count and avoids rewriting a row with its own contents.
        known = _existing_values(session, sorted({r["trip_id"] for r in fact_rows}))
        fact_rows = [
            r
            for r in fact_rows
            if known.get((r["trip_id"], r["stop_sequence"]))
            != tuple(r[name] for name in VALUE_FIELDS)
        ]
        stored = len(fact_rows)
        if fact_rows and not dry_run:
            # The poller owns any stop it has a recorded value for, so it updates
            # in place. finalize only ever fills gaps, and cannot undo this.
            stmt = sqlite_insert(TripStop)
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["trip_id", "stop_sequence"],
                    set_={
                        column: getattr(stmt.excluded, column)
                        for column in fact_rows[0]
                        if column not in ("trip_id", "stop_sequence")
                    },
                ),
                fact_rows,
            )
        if msg_rows and not dry_run:
            # A message repeats on every poll while it is valid.
            session.execute(
                text(
                    "INSERT OR IGNORE INTO tt_message "
                    "(trip_id, stop_sequence, msg_id, msg_type, code, category, ts, "
                    " valid_from, valid_to, priority, deleted) "
                    "VALUES (:trip_id, :stop_sequence, :msg_id, :msg_type, :code, :category, "
                    " :ts, :valid_from, :valid_to, :priority, :deleted)"
                ),
                msg_rows,
            )
            result.messages += len(msg_rows)
        if unmatched_rows and not dry_run:
            session.execute(
                sqlite_insert(TtUnmatched)
                .on_conflict_do_update(
                    index_elements=["service_date", "daily_trip_id", "stop_index"],
                    set_={
                        "last_poll_id": poll.id,
                        "n_polls": TtUnmatched.__table__.c.n_polls + 1,
                    },
                )
                .values(unmatched_rows)
            )

        if seen_trip_ids and not dry_run:
            session.execute(
                update(Trip.__table__)
                .where(Trip.id.in_(list(seen_trip_ids)))
                .values(
                    status=case((Trip.status == "SCHEDULED", "RUNNING"), else_=Trip.status),
                    n_polls_seen=Trip.n_polls_seen + 1,
                )
            )
            for trip_id in seen_trip_ids:
                loaded = session.identity_map.get(session.identity_key(Trip, (trip_id,)))
                if loaded is not None:
                    session.expire(loaded, ["status", "n_polls_seen"])

        result.matched += station_matched
        result.unmatched += station_unmatched
        result.created += station_created
        result.stored += stored
        poll.n_matched = station_matched
        poll.n_unmatched = station_unmatched
        poll.n_changed = stored
        poll.store_ms = int((time.monotonic() - store_started) * 1000)

        station.last_fchg_at = observed_at
        station.n_fchg_polls += 1
        result.stations_polled += 1
        result.stops_seen += parsed.n_raw_stops
        progress(
            f"    {station.name:<26} {parsed.n_raw_stops:>5} stops "
            f"{station_matched:>5} matched {stored:>4} stored  "
            f"{int((time.monotonic() - fetch_started) * 1000)}ms"
        )

    session.flush()
    result.upsert_calls = run_counters.upsert_calls
    result.elapsed_s = time.monotonic() - started
    return result


def _fact_row(
    ref: _TripRef,
    stop: TtStop,
    ar: TtEvent | None,
    dp: TtEvent | None,
    planned: _Planned,
    observed_at: int,
) -> dict:
    """One trip_stop row: what was scheduled, what happened, and how we know."""
    cancelled = (ar is not None and ar.is_cancelled) or (dp is not None and dp.is_cancelled)

    if cancelled:
        # Stated outright by IRIS via cs='c' plus clt, so there is nothing to infer
        # and no delay to contribute.
        actual_arr = actual_dep = arr_delay = dep_delay = None
        quality = QUALITY_CANCELLED
        read_lag = None
    else:
        actual_arr, arr_delay = _resolve(
            ar.changed_ts if ar else None, ar.delay_s if ar else None, planned.supposed_arr
        )
        actual_dep, dep_delay = _resolve(
            dp.changed_ts if dp else None, dp.delay_s if dp else None, planned.supposed_dep
        )
        # The event had already happened when we read it, so this is DB's recorded
        # time rather than a forecast. Nothing to infer.
        quality = QUALITY_RECORDED
        happened = actual_dep or actual_arr
        read_lag = observed_at - happened if happened is not None else None

    return {
        "trip_id": ref.trip_id,
        "stop_sequence": stop.stop_index,
        "station_id": planned.station_id,
        "station_stop_id": planned.station_stop_id,
        "supposed_arrival_utc": planned.supposed_arr,
        "supposed_departure_utc": planned.supposed_dep,
        "actual_arrival_utc": actual_arr,
        "actual_departure_utc": actual_dep,
        "arr_delay_s": arr_delay,
        "dep_delay_s": dep_delay,
        "quality": quality,
        "rule_version": RULE_VERSION_TT,
        "last_observed_at": observed_at,
        "poll_gap_s": None,
        "read_lag_s": read_lag,
        "cancelled": int(cancelled),
        "arr_cancelled": int(ar is not None and ar.is_cancelled),
        "dep_cancelled": int(dp is not None and dp.is_cancelled),
        "cancelled_at_utc": (ar.cancelled_at if ar else None) or (dp.cancelled_at if dp else None),
        "added": int((ar.changed_status if ar else None) == "a"
                     or (dp.changed_status if dp else None) == "a"),
        # cp and pp kept apart: coalescing them made "the platform changed" and
        # "this is the booked platform" indistinguishable downstream.
        "planned_platform": planned.platform_code,
        "changed_platform": (dp.changed_platform if dp else None)
        or (ar.changed_platform if ar else None),
        "is_handover": 0,
        "supposed_departure_hour": local_hour(planned.supposed_dep)
        if planned.supposed_dep
        else None,
    }
