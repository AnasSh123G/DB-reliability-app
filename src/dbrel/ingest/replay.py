"""Serve archived IRIS responses where the live client would serve fetched ones.

`import_plan` and `run_tt_poll` take a client and ask it for `/plan` and `/fchg`.
Nothing else about them cares where the bytes came from, so replaying the archive
is a matter of substituting the source rather than reimplementing the ingest. That
matters: the grading rules those two modules apply -- what counts as happened, what
counts as recorded, what is left for `finalize` to mark as unobserved -- are the
whole reason for backfilling from raw responses instead of the mirror's processed
parquet. A second implementation of them would drift from the first.

The one thing replay must do differently is the clock. Each source is scoped to a
single sweep and carries that sweep's recording instant, which the driver passes
as `now`. See timeutil.read_clock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from dbrel.ingest.archive import ArchivedResponse
from dbrel.ingest.client import Response

# What the live gateway returns for a station with no data in the requested hour,
# and what plan_import already treats as routine rather than as an error.
_ABSENT = 404


@dataclass(slots=True)
class ReplayCounters:
    plan_served: int = 0
    plan_absent: int = 0
    changes_served: int = 0
    changes_absent: int = 0
    # Archived responses for stations this installation does not track. Counted
    # rather than silently dropped: coverage we are declining is worth reporting.
    untracked_evas: set[int] = field(default_factory=set)


class ArchiveReplaySource:
    """One sweep's archived responses, behind the client's own surface.

    Indexed on construction because `run_tt_poll` iterates the *tracked stations*
    and asks for each in turn, while the archive is a bag of whatever the upstream
    job captured. The two orders have to meet somewhere, and a dict is cheaper
    than rescanning the sweep per station.
    """

    def __init__(
        self,
        responses,
        *,
        tracked_evas: set[int] | None = None,
        counters: ReplayCounters | None = None,
    ) -> None:
        self.counters = counters or ReplayCounters()
        # import_plan fetches through a thread pool (TT_FETCH_WORKERS, default 8),
        # so plan() is called concurrently and `+=` on a shared int would drop
        # counts. Only the plan side needs this; the poll loop is sequential.
        self._lock = threading.Lock()
        self._tracked = tracked_evas
        # (eva, yymmdd, hour) -> body, matching client.plan's own URL shape.
        self._plan: dict[tuple[int, str, int], bytes] = {}
        self._changes: dict[int, bytes] = {}
        self._recorded_at: dict[int, int] = {}

        for response in responses:
            if tracked_evas is not None and response.eva not in tracked_evas:
                self.counters.untracked_evas.add(response.eva)
                continue
            if response.is_changes:
                # Later wins: within one sweep a station is normally called once,
                # but a retry would leave two, and the later read is the better one.
                previous = self._recorded_at.get(response.eva)
                if previous is None or response.recorded_at >= previous:
                    self._changes[response.eva] = response.body
                    self._recorded_at[response.eva] = response.recorded_at
            elif response.is_plan:
                key = _plan_key(response)
                if key is not None:
                    self._plan[key] = response.body

    @property
    def evas(self) -> set[int]:
        """Stations this source can answer for at all."""
        return set(self._changes) | {eva for eva, _, _ in self._plan}

    def plan(self, eva: int, date: str, hour: int) -> Response:
        body = self._plan.get((eva, date, int(hour)))
        if body is None:
            with self._lock:
                self.counters.plan_absent += 1
            return Response(status=_ABSENT, body=b"", elapsed_ms=0)
        with self._lock:
            self.counters.plan_served += 1
        return Response(status=200, body=body, elapsed_ms=0)

    def changes(self, eva: int) -> Response:
        body = self._changes.get(eva)
        if body is None:
            # This sweep never read this station. Not an error and not silence
            # from the railway: our own coverage gap, which the poll record and
            # finalize's poll_gap_s are there to express.
            self.counters.changes_absent += 1
            return Response(status=_ABSENT, body=b"", elapsed_ms=0)
        self.counters.changes_served += 1
        return Response(status=200, body=body, elapsed_ms=0)


def _plan_key(response: ArchivedResponse) -> tuple[int, str, int] | None:
    """Recover (eva, yymmdd, hour) from an archived /plan response.

    The date and hour are only in the request URL, not the payload, which is why
    ArchivedResponse keeps them. Rebuilt here rather than in archive.py so the
    archive module stays a reader and this one owns the client's key shape.
    """
    parts = response.plan_date_hour
    if parts is None:
        return None
    date, hour = parts
    return response.eva, date, hour


# --- the driver ------------------------------------------------------------


@dataclass(slots=True)
class ReplayResult:
    days: int = 0
    sweeps: int = 0
    plan_buckets: int = 0
    plan_stops: int = 0
    trips_created: int = 0
    stops_stored: int = 0
    stops_pending: int = 0
    # finalize's share: the stops that produced no value, graded by whether we
    # were listening. Without this pass an unobserved stop is merely absent from
    # the fact table, which is not the same as being recorded as unobserved.
    gaps_written: int = 0
    trips_finalized: int = 0
    # quality -> count, as the fact table ended up holding them.
    by_quality: dict = field(default_factory=dict)
    untracked_evas: set = field(default_factory=set)
    uncovered_tracked: set = field(default_factory=set)
    missing_hours: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def archive_evas(self) -> int:
        return len(self.untracked_evas)


def replay_day(
    session,
    store,
    day,
    *,
    tracked_evas: set[int],
    dry_run: bool = False,
    progress=lambda msg: None,
) -> ReplayResult:
    """Replay one archived day into the database, one upstream job at a time.

    Each partition file *is* one job run: the `/fchg` sweep it took, plus the
    `/plan` hours it fetched. Replaying file by file therefore reconstructs what
    the upstream collector actually did, in order, and keeps memory to one job's
    responses rather than a day's (a whole day held at once cost 2.9 GB).

    Within a file, `/plan` goes first. `/fchg` is a pure delta for ordinary runs
    -- 89.7% of its stops carry no `pt` at all (findings §19) -- so the planned
    side must exist before a change has anything to attach to.

    Each sweep is judged against its own recording instant, so a forecast is seen
    as a forecast and a later sweep supersedes it, exactly as live collection saw
    them. See timeutil.read_clock.
    """
    from dbrel.ingest.archive import (
        covered_hours,
        files_for_date,
        group_sweeps,
        missing_hours,
        read_partition,
        sweep_instant,
    )
    from dbrel.ingest.changes_poller import run_tt_poll
    from dbrel.ingest.finalize_tt import run_finalize_tt
    from dbrel.ingest.plan_import import import_plan

    result = ReplayResult(days=1)
    paths = files_for_date(store, day)
    if not paths:
        result.errors.append(f"{day}: no partitions on disk; run archive-fetch first")
        return result

    result.missing_hours = missing_hours(paths, day, day)
    counters = ReplayCounters()
    yymmdd = day.strftime("%y%m%d")
    last_instant: int | None = None
    sweep_index = 0
    covered_evas: set[int] = set()

    for path in paths:
        plan_by_hour: dict[int, list] = {}
        changes: list = []
        for response in read_partition(path):
            if response.eva not in tracked_evas:
                result.untracked_evas.add(response.eva)
                continue
            if response.is_plan:
                parts = response.plan_date_hour
                # Only this date's buckets. A neighbouring date's plan belongs to
                # that date's replay, and importing it here would duplicate work
                # across consecutive days for no gain.
                if parts is not None and parts[0] == yymmdd:
                    plan_by_hour.setdefault(parts[1], []).append(response)
            else:
                changes.append(response)
                covered_evas.add(response.eva)

        for hour in sorted(plan_by_hour):
            responses = plan_by_hour[hour]
            source = ArchiveReplaySource(responses, tracked_evas=tracked_evas, counters=counters)
            # Stamped with when the archive read this bucket, not when we replayed.
            recorded = max(r.recorded_at for r in responses)
            plan_result = import_plan(
                session,
                source,
                date=day.isoformat(),
                hours=[hour],
                force=True,
                now=recorded,
                progress=lambda _msg: None,
            )
            result.plan_buckets += plan_result.buckets_fetched
            result.plan_stops += plan_result.stops_seen
            result.trips_created += plan_result.trips_created
            result.errors.extend(plan_result.errors[:2])
        session.flush()

        for sweep in group_sweeps(changes):
            instant = sweep_instant(sweep)
            last_instant = max(last_instant or instant, instant)
            source = ArchiveReplaySource(sweep, tracked_evas=tracked_evas, counters=counters)
            poll = run_tt_poll(
                session,
                source,
                # Every tracked station, every sweep: the archive's own cadence is
                # the only tiering there is, and it already applied it upstream.
                priority_max=99,
                sweep_index=sweep_index,
                dry_run=dry_run,
                now=instant,
                progress=lambda _msg: None,
            )
            sweep_index += 1
            result.sweeps += 1
            result.stops_stored += poll.stored
            result.stops_pending += poll.pending
            result.errors.extend(poll.errors[:2])
        session.flush()
        hours = sorted(h for _d, h in covered_hours([path]))
        progress(
            f"  {path.name[:34]:34} hours {hours[0]:02d}-{hours[-1]:02d}  "
            f"stored {result.stops_stored:,}"
        )

    # --- account for the stops that produced nothing ---------------------
    # The poller only ever walks /fchg, so a scheduled stop the archive never
    # reported is invisible to it. Grading that absence is finalize's job, and it
    # is where the guarantee actually lives: NO_DATA when a poll covered the
    # window and nothing appeared, PREDICTION_ONLY when none did, with poll_gap_s
    # measuring how wide the blind spot was. Never a zero.
    #
    # Judged at the last instant this day has evidence for, so a stop whose
    # window is still open is left for the following day's replay rather than
    # written off on the strength of polls that had not happened yet.
    if last_instant is not None and not dry_run:
        finalized = run_finalize_tt(session, now=last_instant, progress=lambda _m: None)
        result.trips_finalized = finalized.trips_finalized
        result.gaps_written = finalized.gaps_written
        for quality, count in finalized.by_quality.items():
            result.by_quality[quality] = result.by_quality.get(quality, 0) + count
        session.flush()

    result.untracked_evas |= counters.untracked_evas
    result.uncovered_tracked = tracked_evas - covered_evas
    return result
