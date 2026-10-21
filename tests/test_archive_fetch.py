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
    ArchiveStore,
    RemoteFile,
    resolve_window,
)

TODAY = date(2026, 9, 22)


# --- task 2.4: the window --------------------------------------------------


def test_an_inverted_window_is_rejected():
    with pytest.raises(ValueError, match="after end"):
        resolve_window(date_from="2026-09-20", date_to="2026-09-10", today=TODAY)


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


