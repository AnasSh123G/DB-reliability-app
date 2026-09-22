"""Conditional download of the static GTFS archives."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from dbrel.config import CACHE_DIR, HTTP_TIMEOUT_S, STATIC_FEEDS


@dataclass(slots=True)
class Download:
    kind: str
    url: str
    path: Path
    content: bytes
    sha256: str
    etag: str | None
    last_modified: str | None
    from_cache: bool

    @property
    def size(self) -> int:
        return len(self.content)


def _meta_path(path: Path) -> Path:
    return path.with_suffix(".meta.json")


def fetch_static(kind: str, force: bool = False) -> Download:
    """Download a static feed, using If-None-Match / If-Modified-Since.

    gtfs.de serves a strong ETag and regenerates roughly weekly, so a 304 is the
    normal outcome of re-running an import within the same week.
    """
    if kind not in STATIC_FEEDS:
        raise ValueError(f"unknown feed {kind!r}; expected one of {sorted(STATIC_FEEDS)}")

    url = STATIC_FEEDS[kind]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{kind}.zip"
    meta_path = _meta_path(path)

    meta: dict[str, str] = {}
    if meta_path.exists() and path.exists() and not force:
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            meta = {}

    headers: dict[str, str] = {}
    if meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]

    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
        response = client.get(url, headers=headers)

    if response.status_code == 304 and path.exists():
        content = path.read_bytes()
        return Download(
            kind=kind,
            url=url,
            path=path,
            content=content,
            sha256=meta.get("sha256") or hashlib.sha256(content).hexdigest(),
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
            from_cache=True,
        )

    response.raise_for_status()
    content = response.content
    digest = hashlib.sha256(content).hexdigest()
    path.write_bytes(content)
    meta_path.write_text(
        json.dumps(
            {
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
                "sha256": digest,
                "fetched_at": int(time.time()),
                "url": url,
            },
            indent=2,
        )
    )
    return Download(
        kind=kind,
        url=url,
        path=path,
        content=content,
        sha256=digest,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        from_cache=False,
    )
