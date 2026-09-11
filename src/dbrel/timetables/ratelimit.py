"""A token bucket shared across processes.

Every dbrel command is a separate one-shot process, and the deployment is two
concurrent cron entries — a /plan roller and a /fchg sweeper. An in-process
limiter would let each of them believe it owned the whole 60 rpm budget, so the
state lives in a file guarded by flock.

The lock is never held across a sleep: hold it only long enough to read, refill
and write, then release, sleep outside, and retry.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path


class TokenBucket:
    def __init__(self, path: Path, rpm: int, *, capacity: int | None = None) -> None:
        self.path = Path(path)
        self.rpm = max(1, rpm)
        self.capacity = capacity if capacity is not None else self.rpm
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.slept_s = 0.0

    def _read_write(self, mutate) -> float:
        """Run mutate(state) under an exclusive lock. Returns seconds to wait."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 4096).decode("utf-8") or "{}"
            try:
                state = json.loads(raw)
            except json.JSONDecodeError:
                state = {}
            wait = mutate(state)
            payload = json.dumps(state).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            return wait
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def acquire(self, n: int = 1) -> float:
        """Block until n tokens are available. Returns total seconds slept."""
        slept = 0.0
        while True:
            def mutate(state, n=n):
                now = time.time()
                tokens = float(state.get("tokens", self.capacity))
                updated = float(state.get("updated_at", now))
                tokens = min(self.capacity, tokens + (now - updated) * self.rpm / 60.0)
                state["updated_at"] = now
                if tokens >= n:
                    state["tokens"] = tokens - n
                    return 0.0
                state["tokens"] = tokens
                return (n - tokens) * 60.0 / self.rpm

            wait = self._read_write(mutate)
            if wait <= 0:
                self.slept_s += slept
                return slept
            time.sleep(wait)
            slept += wait

    def penalise(self, n: int = 1) -> None:
        """Spend tokens without waiting — for a 429, which the server counted."""

        def mutate(state, n=n):
            state["tokens"] = max(0.0, float(state.get("tokens", self.capacity)) - n)
            state["updated_at"] = time.time()
            return 0.0

        self._read_write(mutate)
