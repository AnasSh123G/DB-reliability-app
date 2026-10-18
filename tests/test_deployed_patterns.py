"""A route variant's rows become (start, shared pattern, id runs) and come back exactly."""

from __future__ import annotations

from dbrel.deployed import ScheduleRow, decode_variant, encode_variant, pattern_digest


def _rows(start: int, first_id: int, platforms=("1", "4", "2"), ids=None):
    """A three-stop variant leaving at `start`: origin (departure only), a through stop, a terminus."""
    times = [(None, start), (start + 600, start + 660), (start + 1500, None)]
    ids = ids or [first_id + i for i in range(3)]
    return [ScheduleRow(id=ids[i], stop_sequence=i + 1, station_id=10 + i, source_stop_id=f"s{i}",
                        platform_code=platforms[i], planned_arrival_sec=arr, planned_departure_sec=dep,
                        pickup_type=None, drop_off_type=None)
            for i, (arr, dep) in enumerate(times)]


def _round_trip(rows):
    """Encode `rows`, check they decode back exactly, and return (start, pattern, runs)."""
    start, pattern, runs = encode_variant(rows)
    assert decode_variant(start, pattern, runs) == rows
    return start, pattern, runs


def test_two_variants_an_hour_apart_share_a_pattern():
    """Variants an hour apart share one pattern and keep their starts."""
    a_start, a, _ = _round_trip(_rows(8 * 3600 + 240, first_id=100))
    b_start, b, _ = _round_trip(_rows(9 * 3600 + 240, first_id=500))
    assert a == b and pattern_digest(a) == pattern_digest(b)
    assert (a_start, b_start) == (8 * 3600 + 240, 9 * 3600 + 240)


def test_a_platform_difference_gives_two_patterns():
    """A different platform makes a different pattern."""
    _, a, _ = _round_trip(_rows(8 * 3600, first_id=1))
    _, b, _ = _round_trip(_rows(8 * 3600, first_id=1, platforms=("1", "5", "2")))
    assert pattern_digest(a) != pattern_digest(b)


def test_an_arrival_after_midnight_keeps_its_service_day_time():
    """A 25:10 arrival comes back as 90,600 seconds."""
    rows = _rows(24 * 3600 + 3600, first_id=1)  # leaves 25:00, arrives 25:10 at the through stop
    assert rows[1].planned_arrival_sec == 90_600
    _round_trip(rows)


def test_non_consecutive_ids_round_trip_exactly():
    """Ids out of sequence come back exactly, as several id runs."""
    rows = _rows(8 * 3600, first_id=0, ids=[907, 12, 13])
    _, _, runs = _round_trip(rows)
    assert runs == [(0, 907), (1, 12)]
    _, _, runs = _round_trip(_rows(8 * 3600, first_id=0, ids=[5, 9, 4]))
    assert runs == [(0, 5), (1, 9), (2, 4)]


