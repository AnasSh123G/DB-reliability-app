"""A token bucket shared across processes, with an evidence-driven penalty mode.

Every dbrel command is a separate one-shot process, and the deployment is two
concurrent cron entries — a /plan roller and a /fchg sweeper. An in-process
limiter would let each of them believe it owned the whole budget, so the state
lives in a file guarded by flock.

The lock is never held across a sleep: hold it only long enough to read, refill
and write, then release, sleep outside, and retry.

Two rates, not one. The gateway advertises 60 requests/minute but does not appear
to enforce it (config.py, findings §9), so the bucket runs at TT_RPS_FAST and only
slows down when the gateway actually says no: a 429 calls penalise(), which pins
every process to TT_RPM_PENALTY for TT_PENALTY_S and then lets them return to full
speed by themselves. The penalty lives in the shared file rather than in memory
precisely so that one process being told to slow down slows all of them.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from dbrel.config import TT_PENALTY_S, TT_RPM_PENALTY, TT_RPS_FAST


class TokenBucket:
    def __init__(
        self,
        path: Path,
        rps: float | None = None,
        *,
        penalty_rps: float | None = None,
        penalty_s: float | None = None,
    ) -> None:
        self.path = Path(path)
        self.rps = float(rps if rps is not None else TT_RPS_FAST)
        self.penalty_rps = float(
            penalty_rps if penalty_rps is not None else TT_RPM_PENALTY / 60.0
        )
        self.penalty_s = float(penalty_s if penalty_s is not None else TT_PENALTY_S)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.slept_s = 0.0

    def _rate_and_capacity(self, state: dict, now: float) -> tuple[float, float]:
        """The rate in force right now, and how much burst it may carry.

        Capacity is one second's worth of the fast rate, but exactly 1 while
        penalised: carrying a burst into the penalty would let a backed-off
        process fire the whole next minute's allowance in one go, which is the
        precise behaviour the penalty exists to prevent.
        """
        if now < float(state.get("penalty_until", 0.0)):
            return self.penalty_rps, 1.0
        return self.rps, self.rps

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
                rate, capacity = self._rate_and_capacity(state, now)
                # Clamp to capacity so a request larger than one burst cannot wait
                # forever for tokens the bucket will never hold at once.
                want = min(float(n), capacity)
                tokens = float(state.get("tokens", capacity))
                updated = float(state.get("updated_at", now))
                tokens = min(capacity, tokens + (now - updated) * rate)
                state["updated_at"] = now
                if tokens >= want:
                    state["tokens"] = tokens - want
                    return 0.0
                state["tokens"] = tokens
                return (want - tokens) / rate

            wait = self._read_write(mutate)
            if wait <= 0:
                self.slept_s += slept
                return slept
            time.sleep(wait)
            slept += wait

    def penalise(self, n: int = 1) -> None:
        """Enter the penalty rate — for a 429, which the server counted anyway.

        Zeroes the tokens as well as setting the deadline, so the first call after
        a refusal waits out a full penalty interval rather than going straight
        back out on whatever was left in the bucket.
        """

        def mutate(state, n=n):
            now = time.time()
            state["tokens"] = 0.0
            state["updated_at"] = now
            state["penalty_until"] = max(
                float(state.get("penalty_until", 0.0)), now + self.penalty_s
            )
            return 0.0

        self._read_write(mutate)

    def penalised_until(self) -> float:
        """The epoch second the current penalty expires, or 0.0 if not penalised."""
        captured = {}

        def mutate(state):
            captured["until"] = float(state.get("penalty_until", 0.0))
            return 0.0

        self._read_write(mutate)
        until = captured.get("until", 0.0)
        return until if time.time() < until else 0.0
