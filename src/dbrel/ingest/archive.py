"""Acquire archived IRIS responses from the public deutsche-bahn-data mirror.

The mirror (https://github.com/piebro/deutsche-bahn-data, data on Hugging Face)
has been recording the *same* `/plan` and `/fchg` responses this project fetches,
four times a day, for every German station since 2025-11. Its `raw_data/` holds
the verbatim XML, which is why a backfill can replay it through the normal ingest
instead of needing a second parser.

Only `raw_data/` is used. The mirror also publishes a `monthly_processed_data/`
parquet, and that one is unusable here: its merge falls back
`COALESCE(f.departure_change_time, p.departure_planned_time, ...)`, so a stop with
no `/fchg` report at all is published as `delay_in_min = 0`. Measured over 161,871
stops that had a guaranteed post-event poll, 26.6% of its zeros are fabricated
that way, ranging from 1.5% (NWB) to 55.6% (replacement buses). This project
already holds that absent data never means punctual — `/fchg` publishes punctual
trains explicitly, as `ct == pt` (findings §18) — which is what QUALITY_NO_DATA is
for. The distinction survives in `raw_data/` and nowhere else.

This module only acquires and iterates. Grading belongs to changes_poller, as it
does for live collection.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from dbrel.config import TZ

REPO_ID = "piebro/deutsche-bahn-data"
_TREE = "https://huggingface.co/api/datasets/{repo}/tree/main/{path}"
_RESOLVE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

# All-station coverage begins here. Before it the mirror carries only the largest
# ~100 stations, which is not what this project's national seed wants.
NATIONAL_FROM = date(2025, 11, 1)

DEFAULT_WINDOW_DAYS = 30

# A run departing 23:50 is still going after midnight, and the mirror partitions
# by the day a response was *fetched*. Both neighbours are needed so a run
# crossing either boundary can be assembled whole -- the same reason upstream's
# own process_month.sh pulls the adjacent months.
_EDGE_DAYS = 1

_TIMEOUT_S = 60
_CHUNK = 1 << 20

# Which hours a partition covers is encoded in its FILENAME, against the date in
# that name rather than the partition day it sits under: the 03:17 UTC job files
# `date_2026-09-21_hour_20_21_22_23.parquet` under day=22. Both forms upstream has
# used are matched, so an older partition still reports its coverage.
_DATED_FILE_RE = re.compile(r"(?:^|/)date_(\d{4}-\d{2}-\d{2})_hour_((?:\d{2})(?:_\d{2})*)\.parquet$")
_LEGACY_FILE_RE = re.compile(
    r"(?:^|/)year=(\d{4})/month=(\d{1,2})/day=(\d{1,2})/hour_((?:\d{2})(?:_\d{2})*)\.parquet$"
)


class ArchiveError(RuntimeError):
    """The mirror could not be read, or answered with something unexpected."""


@dataclass(slots=True)
class RemoteFile:
    """One partition file as the mirror describes it."""

    path: str
    size: int
    oid: str

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass(slots=True)
class FetchResult:
    days_requested: int = 0
    days_missing: list[str] = field(default_factory=list)
    files_downloaded: int = 0
    files_reused: int = 0
    bytes_downloaded: int = 0
    bytes_on_disk: int = 0
    errors: list[str] = field(default_factory=list)


# --- the window ------------------------------------------------------------


def resolve_window(
    date_from: str | None = None,
    date_to: str | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    today: date | None = None,
) -> tuple[date, date]:
    """The inclusive day range to acquire, as (start, end).

    Defaults to the last `days` days ending today. `today` is injectable for the
    same reason the ingest's clock is: a default that reads the wall clock cannot
    be tested, and a window is a fact about a run, not about now.

    The range returned is the *data* window. Acquisition widens it by a day either
    side (see `partition_days`); callers that replay should not.
    """
    if today is None:
        today = datetime.now(TZ).date()
    if days < 1:
        raise ValueError("days must be at least 1")

    end = date.fromisoformat(date_to) if date_to else today
    if date_from:
        start = date.fromisoformat(date_from)
    else:
        start = end - timedelta(days=days - 1)

    if start > end:
        raise ValueError(f"window start {start} is after end {end}")
    if end > today:
        # The mirror cannot hold the future, and asking for it would report every
        # such day as a gap rather than as the ordinary edge of the archive.
        end = today
        if start > end:
            raise ValueError("the whole requested window is in the future")
    return start, end


def partition_days(start: date, end: date) -> list[date]:
    """Every day partition needed to cover the window, including both edges."""
    first = start - timedelta(days=_EDGE_DAYS)
    last = end + timedelta(days=_EDGE_DAYS)
    out = []
    cursor = first
    while cursor <= last:
        out.append(cursor)
        cursor += timedelta(days=1)
    return out


def day_prefix(day: date) -> str:
    """The mirror's partition path. Month and day are NOT zero padded."""
    return f"raw_data/year={day.year}/month={day.month}/day={day.day}"


# --- the mirror ------------------------------------------------------------


def _get_json(url: str) -> list | dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dbrel"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise ArchiveError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ArchiveError(f"{url}: {exc}") from exc


def list_day(day: date) -> list[RemoteFile]:
    """The parquet files the mirror holds for one day. Empty if the day is absent.

    An absent day is not an error: the archive has real holes, and upstream
    documents 196 missing hours between 2025-11 and 2026-07. A caller reports it
    as a gap rather than failing.
    """
    entries = _get_json(_TREE.format(repo=REPO_ID, path=day_prefix(day)))
    if not isinstance(entries, list):
        raise ArchiveError(f"unexpected tree response for {day}")
    out = []
    for entry in entries:
        if entry.get("type") != "file" or not entry.get("path", "").endswith(".parquet"):
            continue
        lfs = entry.get("lfs") or {}
        out.append(
            RemoteFile(
                path=entry["path"],
                size=int(lfs.get("size") or entry.get("size") or 0),
                oid=str(lfs.get("oid") or entry.get("oid") or ""),
            )
        )
    return sorted(out, key=lambda f: f.name)


# --- local store -----------------------------------------------------------


class ArchiveStore:
    """The downloaded partitions, plus a manifest of what is known intact.

    The manifest is what makes a run resumable: a file whose size and upstream oid
    still match is reused rather than fetched again. Size alone would accept a
    truncated download that happened to stop on a byte boundary; the oid is the
    mirror's own content id for the file.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._manifest_path = self.root / "manifest.json"
        self._manifest: dict[str, dict] = {}
        if self._manifest_path.exists():
            try:
                self._manifest = json.loads(self._manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                # A corrupt manifest costs a re-download, not correctness.
                self._manifest = {}

    def local_path(self, remote: RemoteFile) -> Path:
        return self.root / remote.path

    def is_intact(self, remote: RemoteFile) -> bool:
        record = self._manifest.get(remote.path)
        if not record:
            return False
        path = self.local_path(remote)
        if not path.exists():
            return False
        if record.get("oid") != remote.oid or record.get("size") != remote.size:
            return False
        return path.stat().st_size == remote.size

    def record(self, remote: RemoteFile) -> None:
        self._manifest[remote.path] = {"oid": remote.oid, "size": remote.size}

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._manifest, indent=1, sort_keys=True))
        os.replace(tmp, self._manifest_path)

    def files_for_day(self, day: date) -> list[Path]:
        """Partitions already on disk for one day, chronologically by filename."""
        directory = self.root / day_prefix(day)
        if not directory.is_dir():
            return []
        return sorted(directory.glob("*.parquet"))

    def bytes_on_disk(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*.parquet"))


def download(remote: RemoteFile, store: ArchiveStore) -> int:
    """Fetch one partition into the store. Returns bytes written.

    Written to a sibling temp file and renamed, so an interrupted download never
    leaves a short file that a later run would have to be told to distrust.
    """
    target = store.local_path(remote)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".parquet.part")
    url = _RESOLVE.format(repo=REPO_ID, path=remote.path)
    request = urllib.request.Request(url, headers={"User-Agent": "dbrel"})
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response, tmp.open("wb") as fh:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        raise ArchiveError(f"{remote.path}: {exc}") from exc

    if remote.size and written != remote.size:
        tmp.unlink(missing_ok=True)
        raise ArchiveError(f"{remote.path}: got {written} bytes, expected {remote.size}")

    os.replace(tmp, target)
    store.record(remote)
    return written


def fetch_window(
    store: ArchiveStore,
    start: date,
    end: date,
    *,
    progress=lambda msg: None,
    on_progress=lambda done, total: None,
) -> FetchResult:
    """Acquire every partition covering the window. Resumable and idempotent."""
    result = FetchResult()
    days = partition_days(start, end)
    result.days_requested = len(days)

    for done, day in enumerate(days, start=1):
        on_progress(done, len(days))
        if day < NATIONAL_FROM:
            progress(f"  {day}: before {NATIONAL_FROM} (top ~100 stations only), skipped")
            continue
        try:
            remotes = list_day(day)
        except ArchiveError as exc:
            result.errors.append(str(exc))
            continue
        if not remotes:
            result.days_missing.append(day.isoformat())
            progress(f"  {day}: absent from the archive")
            continue

        for remote in remotes:
            if store.is_intact(remote):
                result.files_reused += 1
                continue
            try:
                written = download(remote, store)
            except ArchiveError as exc:
                result.errors.append(str(exc))
                continue
            result.files_downloaded += 1
            result.bytes_downloaded += written
            progress(f"  {day} {remote.name} ({written / 1e6:.1f} MB)")
        store.save()

    on_progress(len(days), len(days))
    result.bytes_on_disk = store.bytes_on_disk()
    return result


# --- coverage --------------------------------------------------------------


def covered_hours(paths) -> set[tuple[date, int]]:
    """The (day, hour) buckets a set of partition paths claims to cover.

    Read from the filename rather than by opening each file: the upstream job
    names a partition for exactly the hours it fetched, and a 600 MB parquet
    should not have to be decoded to learn which hour it holds.
    """
    covered: set[tuple[date, int]] = set()
    for raw in paths:
        text = str(raw).replace("\\", "/")
        match = _DATED_FILE_RE.search(text)
        if match:
            day = date.fromisoformat(match.group(1))
            hours = match.group(2)
        else:
            match = _LEGACY_FILE_RE.search(text)
            if not match:
                continue
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            hours = match.group(4)
        for hour in hours.split("_"):
            covered.add((day, int(hour)))
    return covered


def missing_hours(paths, start: date, end: date) -> list[tuple[date, int]]:
    """Hours of [start, end] that no acquired partition covers, in order.

    A gap here is the archive's, not ours. Upstream documents 196 such hours
    between 2025-11 and 2026-07, and more since. Replay turns each into a
    measured poll gap rather than a silent absence -- but an operator has to be
    able to see the hole rather than infer it from a thin day.
    """
    covered = covered_hours(paths)
    out = []
    cursor = start
    while cursor <= end:
        for hour in range(24):
            if (cursor, hour) not in covered:
                out.append((cursor, hour))
        cursor += timedelta(days=1)
    return out


def store_paths(store: "ArchiveStore", start: date, end: date) -> list[Path]:
    """Every acquired partition that could carry an hour of the window."""
    out: list[Path] = []
    for day in partition_days(start, end):
        out.extend(store.files_for_day(day))
    return out


def file_date(path: Path | str) -> date | None:
    """The single date a partition's filename is for, or None if unrecognised.

    Each partition is one upstream job run, and its name carries exactly one
    date -- `date_2026-09-21_hour_00_01_02_03.parquet` is the 23:2x job fetching
    the 21st's first four hours. So a file belongs to one day, and a day's files
    are found by name rather than by which directory they happen to sit in.
    """
    days = {day for day, _hour in covered_hours([path])}
    return days.pop() if len(days) == 1 else None


def files_for_date(store: "ArchiveStore", day: date) -> list[Path]:
    """The partitions covering one date, in the order the jobs ran.

    Sorted by first covered hour, which is the order upstream wrote them, and
    therefore the order their /fchg sweeps were recorded in.
    """
    matches = [p for p in store_paths(store, day, day) if file_date(p) == day]

    def first_hour(path: Path) -> int:
        hours = [hour for _d, hour in covered_hours([path])]
        return min(hours) if hours else 99

    return sorted(matches, key=first_hour)


# --- reading a partition ---------------------------------------------------

_API_PREFIX = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/"
API_PLAN = "timetables/v1/plan"
API_FCHG = "timetables/v1/fchg"

_REQUIRED_COLUMNS = ("timestamp", "url", "api_name", "response_data", "status_code")


@dataclass(slots=True)
class ArchivedResponse:
    """One recorded API response, with the instant it was recorded.

    `recorded_at` is the whole point: it is what replay must judge the payload
    against. The mirror stores it as a naive Europe/Berlin timestamp, so it is
    converted here, once, at the boundary.
    """

    recorded_at: int
    api_name: str
    eva: int
    body: bytes
    # /plan is requested per station per hour, and which hour is only in the
    # request URL -- the payload never says. A replay has to answer the same
    # (eva, date, hour) question the live client was asked, so it is kept here.
    plan_date: str | None = None
    plan_hour: int | None = None

    @property
    def is_plan(self) -> bool:
        return self.api_name == API_PLAN

    @property
    def is_changes(self) -> bool:
        return self.api_name == API_FCHG

    @property
    def plan_date_hour(self) -> tuple[str, int] | None:
        """(yymmdd, hour) as client.plan would be asked for it."""
        if self.plan_date is None or self.plan_hour is None:
            return None
        return self.plan_date, self.plan_hour


def eva_of(url: str) -> int | None:
    """The station an archived request was for.

    `/plan/{eva}/{date}/{hour}` and `/fchg/{eva}`, both zero padded upstream
    ('08001506'), while this project stores evas as plain integers (8001506).
    """
    rest = url.removeprefix(_API_PREFIX)
    if rest is url and _API_PREFIX not in url:
        # Not a timetables URL at all (the mirror also records station-data calls).
        if "/timetables/v1/" not in url:
            return None
        rest = url.split("/timetables/v1/", 1)[1]
    for endpoint in ("plan/", "fchg/"):
        if rest.startswith(endpoint):
            candidate = rest[len(endpoint) :].split("/")[0].split("?")[0]
            return int(candidate) if candidate.isdigit() else None
    return None


def plan_date_hour_of(url: str) -> tuple[str, int] | None:
    """The (yymmdd, hour) a /plan request covered: '/plan/{eva}/{date}/{hour}'."""
    rest = url.split("/timetables/v1/", 1)[-1]
    if not rest.startswith("plan/"):
        return None
    parts = rest[len("plan/") :].split("?")[0].split("/")
    if len(parts) < 3:
        return None
    date, hour = parts[1], parts[2]
    if len(date) != 6 or not date.isdigit() or not hour.isdigit():
        return None
    return date, int(hour)


def _to_epoch(value) -> int | None:
    """A partition's naive Europe/Berlin timestamp -> epoch seconds."""
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        # Naive, as the mirror writes it. fold=0 on the ambiguous autumn hour, the
        # same convention parser.parse_ts uses for IRIS's own timestamps.
        moment = moment.replace(tzinfo=TZ)
    return int(moment.timestamp())


def read_partition(path: Path | str, *, keep_evas: set[int] | None = None):
    """Yield the usable responses in one partition.

    A generator over `iter_batches`, not one `read_table`: a 25 MB partition
    expands to ~0.6 GB peak if fully materialised, and the largest are 60 MB.
    Streaming keeps a backfill's footprint flat regardless of partition size.

    pyarrow is imported here rather than at module scope: it is a ~40 MB optional
    extra (see pyproject.toml), and nothing else in the project may pull it in.

    Non-200 rows are dropped. A failed upstream request is real information about
    coverage, but it carries no payload to replay, and the hour it left uncovered
    is already reported by `missing_hours`.

    Order is not guaranteed; callers that need chronology sort what they keep
    (`group_sweeps` does).
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ArchiveError(
            "reading the archive needs pyarrow: uv sync --extra backfill"
        ) from exc

    handle = pq.ParquetFile(path)
    absent = [c for c in _REQUIRED_COLUMNS if c not in handle.schema_arrow.names]
    if absent:
        raise ArchiveError(f"{path}: missing column(s) {', '.join(absent)}")

    for batch in handle.iter_batches(batch_size=2048, columns=list(_REQUIRED_COLUMNS)):
        for row in batch.to_pylist():
            if str(row.get("status_code")) != "200":
                continue
            body = row.get("response_data")
            if not body:
                continue
            api_name = row.get("api_name") or ""
            if api_name not in (API_PLAN, API_FCHG):
                continue
            eva = eva_of(row.get("url") or "")
            if eva is None or (keep_evas is not None and eva not in keep_evas):
                continue
            recorded_at = _to_epoch(row.get("timestamp"))
            if recorded_at is None:
                continue
            url = row.get("url") or ""
            plan_parts = plan_date_hour_of(url) if api_name == API_PLAN else None
            yield ArchivedResponse(
                recorded_at=recorded_at,
                api_name=api_name,
                eva=eva,
                body=body.encode() if isinstance(body, str) else body,
                plan_date=plan_parts[0] if plan_parts else None,
                plan_hour=plan_parts[1] if plan_parts else None,
            )


# --- sweeps ----------------------------------------------------------------

# The upstream job calls every station in one burst, so a sweep's responses land
# within a few minutes of each other (measured: 00:54-00:58 and 15:20-15:25 on
# 2026-08-28), while sweeps themselves are ~6h apart. Anything under an hour
# separates them safely; 30 minutes leaves room for a slow burst without ever
# merging two sweeps.
SWEEP_GAP_S = 30 * 60


def group_sweeps(responses, gap_s: int = SWEEP_GAP_S) -> list[list[ArchivedResponse]]:
    """Cluster /fchg responses into the sweeps that produced them.

    Each sweep is replayed as one poll, against its own instant, because that is
    what it was: one pass over the stations at one moment. Replaying a whole day
    under a single instant would erase the forecast/recorded distinction that the
    grading depends on.
    """
    changes = sorted(
        (r for r in responses if r.is_changes), key=lambda r: r.recorded_at
    )
    sweeps: list[list[ArchivedResponse]] = []
    for response in changes:
        if sweeps and response.recorded_at - sweeps[-1][-1].recorded_at <= gap_s:
            sweeps[-1].append(response)
        else:
            sweeps.append([response])
    return sweeps


def sweep_instant(sweep) -> int:
    """The instant to judge a sweep's payloads against: its median recording time.

    The median rather than the first or last, so one straggling station cannot
    drag the whole sweep's reference point past an event boundary.
    """
    stamps = sorted(r.recorded_at for r in sweep)
    return stamps[len(stamps) // 2]
