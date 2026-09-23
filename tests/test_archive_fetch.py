"""Window resolution and resumable acquisition for the archive backfill.

The window is a fact about a run, not about now, so `resolve_window` takes the
day to measure from rather than reading the clock. Everything here is offline:
the mirror is only reached through `list_day`/`download`, both of which are
substituted.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dbrel.ingest import archive
from dbrel.ingest.archive import (
    NATIONAL_FROM,
    ArchiveStore,
    RemoteFile,
    day_prefix,
    partition_days,
    resolve_window,
)

TODAY = date(2026, 9, 22)


# --- task 2.4: the window --------------------------------------------------


def test_default_window_is_the_last_30_days_ending_today():
    start, end = resolve_window(today=TODAY)
    assert end == TODAY
    assert start == date(2026, 8, 24)
    assert (end - start).days == 29, "30 days inclusive"


def test_explicit_window_is_honoured():
    start, end = resolve_window(date_from="2026-08-01", date_to="2026-08-31", today=TODAY)
    assert (start, end) == (date(2026, 8, 1), date(2026, 8, 31))


def test_date_from_alone_runs_to_today():
    start, end = resolve_window(date_from="2026-09-20", today=TODAY)
    assert (start, end) == (date(2026, 9, 20), TODAY)


def test_date_to_alone_takes_the_default_length_back_from_it():
    start, end = resolve_window(date_to="2026-08-31", today=TODAY)
    assert (start, end) == (date(2026, 8, 2), date(2026, 8, 31))


def test_a_custom_length_is_honoured():
    start, end = resolve_window(days=7, today=TODAY)
    assert (start, end) == (date(2026, 9, 16), TODAY)


def test_the_future_is_clamped_to_today_rather_than_reported_as_gaps():
    """Asking past today would otherwise mark every such day 'absent'."""
    start, end = resolve_window(date_from="2026-09-20", date_to="2026-12-01", today=TODAY)
    assert (start, end) == (date(2026, 9, 20), TODAY)


def test_an_inverted_window_is_rejected():
    with pytest.raises(ValueError, match="after end"):
        resolve_window(date_from="2026-09-20", date_to="2026-09-10", today=TODAY)


def test_a_window_entirely_in_the_future_is_rejected():
    with pytest.raises(ValueError, match="future"):
        resolve_window(date_from="2026-10-01", date_to="2026-10-05", today=TODAY)


def test_zero_days_is_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        resolve_window(days=0, today=TODAY)


def test_partitions_extend_one_day_either_side_for_overnight_runs():
    """A run departing 23:50 lands in the next day's partition."""
    days = partition_days(date(2026, 9, 20), date(2026, 9, 21))
    assert days == [
        date(2026, 9, 19),
        date(2026, 9, 20),
        date(2026, 9, 21),
        date(2026, 9, 22),
    ]


def test_partition_paths_are_not_zero_padded():
    """The mirror writes month=9/day=1, not month=09/day=01."""
    assert day_prefix(date(2026, 9, 1)) == "raw_data/year=2026/month=9/day=1"
    assert day_prefix(date(2026, 12, 31)) == "raw_data/year=2026/month=12/day=31"


# --- task 2.3: the store resumes ------------------------------------------


def _remote(name: str, size: int = 100, oid: str = "abc") -> RemoteFile:
    return RemoteFile(path=f"raw_data/year=2026/month=9/day=20/{name}", size=size, oid=oid)


def test_a_file_is_intact_only_when_size_and_oid_both_match(tmp_path: Path):
    store = ArchiveStore(tmp_path)
    remote = _remote("a.parquet", size=4, oid="oid-1")
    assert not store.is_intact(remote), "nothing downloaded yet"

    path = store.local_path(remote)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    assert not store.is_intact(remote), "on disk but not yet recorded"

    store.record(remote)
    assert store.is_intact(remote)

    # Upstream republished the partition: same name, different content.
    assert not store.is_intact(_remote("a.parquet", size=4, oid="oid-2"))
    # A truncated file that the manifest still vouches for must be caught.
    path.write_bytes(b"da")
    assert not store.is_intact(remote)


def test_the_manifest_survives_a_restart(tmp_path: Path):
    remote = _remote("a.parquet", size=4, oid="oid-1")
    store = ArchiveStore(tmp_path)
    path = store.local_path(remote)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    store.record(remote)
    store.save()

    assert ArchiveStore(tmp_path).is_intact(remote), "a new process must reuse it"


def test_a_corrupt_manifest_costs_a_redownload_not_a_crash(tmp_path: Path):
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "manifest.json").write_text("{not json")
    store = ArchiveStore(tmp_path)
    assert not store.is_intact(_remote("a.parquet"))


def test_fetch_downloads_then_reuses(tmp_path: Path, monkeypatch):
    """The second run over the same window must download nothing."""
    remotes = [_remote("a.parquet", size=4, oid="oid-a")]
    calls: list[str] = []

    monkeypatch.setattr(
        archive, "list_day", lambda day: remotes if day == date(2026, 9, 20) else []
    )

    def fake_download(remote: RemoteFile, store: ArchiveStore) -> int:
        calls.append(remote.path)
        target = store.local_path(remote)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"data")
        store.record(remote)
        return remote.size

    monkeypatch.setattr(archive, "download", fake_download)

    store = ArchiveStore(tmp_path)
    first = archive.fetch_window(store, date(2026, 9, 20), date(2026, 9, 20))
    assert first.files_downloaded == 1
    assert first.files_reused == 0
    assert first.bytes_downloaded == 4
    assert len(calls) == 1

    second = archive.fetch_window(ArchiveStore(tmp_path), date(2026, 9, 20), date(2026, 9, 20))
    assert second.files_downloaded == 0
    assert second.files_reused == 1
    assert len(calls) == 1, "nothing may be re-downloaded"


def test_an_absent_day_is_reported_as_a_gap_not_an_error(tmp_path: Path, monkeypatch):
    """The archive has real holes; a hole is data about coverage."""
    monkeypatch.setattr(archive, "list_day", lambda day: [])
    result = archive.fetch_window(ArchiveStore(tmp_path), date(2026, 9, 20), date(2026, 9, 20))
    assert result.errors == []
    # The window plus both edges.
    assert result.days_missing == ["2026-09-19", "2026-09-20", "2026-09-21"]


def test_days_before_national_coverage_are_skipped(tmp_path: Path, monkeypatch):
    """Before 2025-11 the mirror holds only the largest ~100 stations."""
    seen: list[date] = []

    def spy(day: date):
        seen.append(day)
        return []

    monkeypatch.setattr(archive, "list_day", spy)
    before = NATIONAL_FROM.replace(day=2)
    archive.fetch_window(
        ArchiveStore(tmp_path), NATIONAL_FROM.fromordinal(before.toordinal() - 4), before
    )
    assert all(day >= NATIONAL_FROM for day in seen), seen


# --- task 2.6: coverage and gaps ------------------------------------------

# The real partition names for 2026-08-28, from the live mirror listing. Hours
# 05-07 are genuinely absent -- the gap that showed up as a 5x drop in that day's
# stop count, and the reason an archive hole must never read as punctual running.
AUG28 = [
    "raw_data/year=2026/month=8/day=28/date_2026-08-27_hour_19_20_21_22_23.parquet",
    "raw_data/year=2026/month=8/day=28/date_2026-08-28_hour_00_01_02_03_04.parquet",
    "raw_data/year=2026/month=8/day=28/"
    "date_2026-08-28_hour_08_09_10_11_12_13_14_15_16_17_18_19.parquet",
]


def test_covered_hours_are_read_from_the_filename_not_the_partition_day():
    """date_2026-08-27_hour_19.. sits under day=28 but covers the 27th."""
    covered = archive.covered_hours(AUG28)
    assert (date(2026, 8, 27), 19) in covered
    assert (date(2026, 8, 27), 23) in covered
    assert (date(2026, 8, 28), 0) in covered


def test_the_known_aug_28_gap_is_reported():
    missing = archive.missing_hours(AUG28, date(2026, 8, 28), date(2026, 8, 28))
    hours = [hour for _, hour in missing]
    assert hours[:3] == [5, 6, 7], "the documented hole in this day"
    assert 4 not in hours and 8 not in hours, "the hours either side were captured"


def test_hours_filed_under_the_next_day_are_not_reported_missing():
    """Evening hours land in the following day's partition; edges cover them."""
    paths = AUG28 + [
        "raw_data/year=2026/month=8/day=29/date_2026-08-28_hour_20_21_22_23.parquet"
    ]
    hours = [hour for _, hour in archive.missing_hours(paths, date(2026, 8, 28), date(2026, 8, 28))]
    assert hours == [5, 6, 7], "only the real hole remains"


def test_a_fully_covered_day_reports_nothing_missing():
    paths = [
        "raw_data/year=2026/month=9/day=1/date_2026-09-01_hour_"
        + "_".join(f"{h:02d}" for h in range(24))
        + ".parquet"
    ]
    assert archive.missing_hours(paths, date(2026, 9, 1), date(2026, 9, 1)) == []


def test_the_legacy_partition_layout_still_reports_coverage():
    paths = ["raw_data/year=2025/month=11/day=3/hour_00_01_02.parquet"]
    covered = archive.covered_hours(paths)
    assert covered == {(date(2025, 11, 3), h) for h in (0, 1, 2)}


def test_unrecognised_filenames_are_ignored_not_guessed_at():
    assert archive.covered_hours(["raw_data/README.md", "manifest.json"]) == set()
