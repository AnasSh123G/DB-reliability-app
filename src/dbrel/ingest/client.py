"""HTTP access to the DB Timetables API (IRIS-TTS).

Mirrors the role of gtfs/download.py: everything that touches the network lives
here, so the parser stays pure and testable.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx

from dbrel.config import (
    TT_API_KEY,
    TT_API_KEY_SOURCE,
    TT_BASE_URL,
    TT_CLIENT_ID,
    TT_CLIENT_ID_SOURCE,
    TT_HTTP_TIMEOUT_S,
    TT_RATELIMIT_STATE,
    TT_RPS_FAST,
)
from dbrel.ingest.ratelimit import TokenBucket

MAX_RETRIES = 4


class MissingCredentials(RuntimeError):
    pass


@dataclass(slots=True)
class Response:
    status: int
    body: bytes
    elapsed_ms: int
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def rate_limit(self) -> dict[str, str]:
        return {k: v for k, v in self.headers.items() if "ratelimit" in k.lower()}


class TimetablesClient:
    """Rate-limited, retrying client. One instance per command invocation."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        api_key: str | None = None,
        rps: float = TT_RPS_FAST,
        base_url: str = TT_BASE_URL,
        timeout: float = TT_HTTP_TIMEOUT_S,
    ) -> None:
        self.client_id = client_id or TT_CLIENT_ID
        self.api_key = api_key or TT_API_KEY
        self.base_url = base_url.rstrip("/")
        self.bucket = TokenBucket(TT_RATELIMIT_STATE, rps)
        # Shared by every thread in the fetch pool: httpx.Client is thread-safe and
        # pools connections, which is what makes concurrent /plan fetches worthwhile.
        self.http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
        )
        # get() is called from the /plan fetch pool, so the counters need a lock:
        # `+=` is a read-modify-write and would silently undercount otherwise.
        self._counter_lock = threading.Lock()
        self.n_requests = 0
        self.n_retries = 0

    def require_credentials(self) -> None:
        missing = []
        if not self.client_id:
            missing.append("client id (tried DBREL_TT_CLIENT_ID, DB_CLIENT_ID)")
        if not self.api_key:
            missing.append("api key (tried DBREL_TT_API_KEY, TIMETABLES_API_KEY, DB_API_KEY)")
        if missing:
            raise MissingCredentials(
                "Timetables credentials incomplete: "
                + "; ".join(missing)
                + ". Both DB-Client-Id and DB-Api-Key are required."
            )

    def headers(self, *, with_id: bool = True, with_key: bool = True) -> dict[str, str]:
        h = {"Accept": "application/xml"}
        if with_id and self.client_id:
            h["DB-Client-Id"] = self.client_id
        if with_key and self.api_key:
            h["DB-Api-Key"] = self.api_key
        return h

    def _count(self, *, requests: int = 0, retries: int = 0) -> None:
        with self._counter_lock:
            self.n_requests += requests
            self.n_retries += retries

    def get(self, path: str, *, with_id: bool = True, with_key: bool = True) -> Response:
        """GET path relative to the API root, respecting the shared rate limit.

        A 5xx or 429 is retried; a 404 is returned as-is, because /plan legitimately
        404s for an hour bucket with no service or beyond the horizon.

        Safe to call from several threads at once: the bucket serialises on a file
        lock, httpx.Client pools connections, and the counters take a lock.
        """
        url = self.base_url + path
        headers = self.headers(with_id=with_id, with_key=with_key)
        backoff = 2.0
        for attempt in range(MAX_RETRIES):
            self.bucket.acquire()
            started = time.monotonic()
            try:
                resp = self.http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES - 1:
                    return Response(0, str(exc).encode(), int((time.monotonic() - started) * 1000))
                self._count(retries=1)
                time.sleep(backoff)
                backoff *= 2
                continue

            self._count(requests=1)
            elapsed = int((time.monotonic() - started) * 1000)
            out = Response(resp.status_code, resp.content, elapsed, dict(resp.headers))

            if resp.status_code == 429:
                # The server counted this request even though it served nothing, and
                # it is the only direct evidence that the advertised limit is real —
                # so it drops every process to the penalty rate, not just this call.
                self.bucket.penalise()
                self._count(retries=1)
                wait = float(resp.headers.get("Retry-After", backoff))
                time.sleep(wait)
                backoff *= 2
                continue
            if resp.status_code >= 500 and attempt < MAX_RETRIES - 1:
                self._count(retries=1)
                time.sleep(backoff)
                backoff *= 2
                continue
            return out
        return out

    # -- endpoints ---------------------------------------------------------

    def station(self, pattern: str) -> Response:
        return self.get(f"/station/{pattern}")

    def plan(self, eva: int, date: str, hour: int) -> Response:
        """date is YYMMdd, hour 0-23. Serves today and tomorrow only (findings §15)."""
        return self.get(f"/plan/{eva}/{date}/{hour:02d}")

    def changes(self, eva: int) -> Response:
        """All known changes from now forward. Refreshed every 30 s upstream."""
        return self.get(f"/fchg/{eva}")

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> TimetablesClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def credential_report() -> list[str]:
    """Human-readable provenance of the credentials, without printing them."""
    return [
        f"client id: {'set' if TT_CLIENT_ID else 'MISSING'} "
        f"({len(TT_CLIENT_ID or '')} chars, from {TT_CLIENT_ID_SOURCE})",
        f"api key  : {'set' if TT_API_KEY else 'MISSING'} "
        f"({len(TT_API_KEY or '')} chars, from {TT_API_KEY_SOURCE})",
    ]
