"""The adaptive token bucket.

It runs fast by default and slows down only on evidence -- a 429 -- then recovers
by itself. Both halves matter: never slowing down means a real limit turns into a
wall of failed requests, and never recovering means one 429 costs the rest of the
run. The timings here are asserted against a bucket built with tiny rates so the
test stays in milliseconds rather than sleeping through a real three-minute penalty.
"""

from __future__ import annotations

import json
import time

from dbrel.timetables.ratelimit import TokenBucket


def _bucket(tmp_path, rps=1000.0, penalty_rps=50.0, penalty_s=0.3) -> TokenBucket:
    return TokenBucket(
        tmp_path / "rl.json", rps, penalty_rps=penalty_rps, penalty_s=penalty_s
    )


def test_fast_mode_does_not_sleep_within_one_seconds_burst(tmp_path):
    bucket = _bucket(tmp_path, rps=1000.0)
    started = time.monotonic()
    for _ in range(50):
        bucket.acquire()
    assert time.monotonic() - started < 0.5, "a burst inside capacity must not pace"


def test_penalise_records_a_deadline_in_the_shared_file(tmp_path):
    bucket = _bucket(tmp_path, penalty_s=60.0)
    assert bucket.penalised_until() == 0.0
    bucket.penalise()

    state = json.loads((tmp_path / "rl.json").read_text())
    assert state["penalty_until"] > time.time()
    assert state["tokens"] == 0.0, "the bucket is emptied, not just slowed"
    assert bucket.penalised_until() > 0.0


def test_a_penalty_applies_to_every_process_sharing_the_file(tmp_path):
    """Separate instances are separate processes in production."""
    one = _bucket(tmp_path, penalty_s=60.0)
    two = _bucket(tmp_path, penalty_s=60.0)
    one.penalise()
    assert two.penalised_until() > 0.0


def test_penalty_paces_calls_that_fast_mode_would_have_waved_through(tmp_path):
    bucket = _bucket(tmp_path, rps=1000.0, penalty_rps=20.0, penalty_s=5.0)
    bucket.penalise()

    started = time.monotonic()
    bucket.acquire()
    elapsed = time.monotonic() - started
    # Emptied bucket at 20/s means the next token is ~50 ms away.
    assert elapsed >= 0.04, f"penalised call went through in {elapsed:.3f}s"


def test_the_penalty_expires_on_its_own(tmp_path):
    bucket = _bucket(tmp_path, rps=1000.0, penalty_rps=20.0, penalty_s=0.2)
    bucket.penalise()
    assert bucket.penalised_until() > 0.0

    time.sleep(0.25)
    assert bucket.penalised_until() == 0.0, "recovery must not need another call"

    started = time.monotonic()
    for _ in range(20):
        bucket.acquire()
    assert time.monotonic() - started < 0.3, "back to fast mode after expiry"


def test_a_second_penalty_extends_but_never_shortens_the_first(tmp_path):
    bucket = _bucket(tmp_path, penalty_s=60.0)
    bucket.penalise()
    first = bucket.penalised_until()

    short = _bucket(tmp_path, penalty_s=0.1)
    short.penalise()
    assert short.penalised_until() >= first - 1, (
        "a shorter penalty must not cut a longer one short"
    )


def test_a_corrupt_state_file_does_not_wedge_the_limiter(tmp_path):
    (tmp_path / "rl.json").write_text("{not json")
    bucket = _bucket(tmp_path)
    bucket.acquire()  # must not raise
