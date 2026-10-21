import pytest
from planner_fixture import make_timetable, make_late_connection_timetable
from test_planner_search import epoch
from dbrel.planner.forecast import FixedForecaster
from dbrel.planner.search import Budget
from dbrel.planner.routes import (
    plan_chain,
    plan_chain_before,
    plan_routes,
    route_key,
    route_instances,
    parse_route_key,
    tighten,
)


@pytest.mark.parametrize(
    "delays,keys",
    [
        ({}, ["pl100@1>2~pl200@2>4", "cICE@1>4", "pl100@1>3~pl400@3>4"]),
        (
            {(100, 2): (4, 4, 4)},
            ["cICE@1>4", "pl100@1>2~pl200@2>4", "pl100@1>3~pl400@3>4"],
        ),
    ],
)
def test_distinct(delays, keys):
    tt = make_timetable()
    fc = FixedForecaster(tt, delays)
    cache = {}
    accepted, limited, n = plan_routes(tt, fc, 0, 3, epoch(), Budget(), cache=cache)
    assert [k for _, k in accepted] == keys
    assert not limited
    assert len(fc.keys) == len(set(fc.keys))


def test_sunday():
    tt = make_timetable()
    fc = FixedForecaster(tt, {})
    cache = {}
    accepted, _, _ = plan_routes(
        tt, fc, 0, 3, epoch("2026-10-18"), Budget(), cache=cache
    )
    assert [k for _, k in accepted] == ["pl100@1>2~pl200@2>4", "pl100@1>3~pl400@3>4"]


@pytest.mark.parametrize(
    "key",
    [
        "pl100@1>2~pl200@3>4",
        "x@1>4",
        "pl100@2>4",
        "~".join(["pl100@1>1"] * 5 + ["pl100@1>4"]),
    ],
)
def test_invalid(key):
    assert parse_route_key(make_timetable(), key, 1, 4) is None


@pytest.mark.parametrize(
    "delays,count,first", [({}, 1, 100), ({(100, 2): (4,) * 3}, 2, 102)]
)
def test_instances(delays, count, first):
    tt = make_timetable()
    fc = FixedForecaster(tt, delays)
    spec = parse_route_key(tt, "pl100@1>2~pl200@2>4", 1, 4)
    instances = route_instances(
        tt, fc, spec, epoch(clock="06:00"), epoch(clock="10:00")
    )
    assert len(instances) == count
    assert tt.var_train[instances[0][0].variant] == first


def test_tightening():
    tt = make_timetable()
    fc = FixedForecaster(tt, {})
    cache = {}
    accepted, _, _ = plan_routes(
        tt, fc, 0, 3, epoch(clock="07:00"), Budget(), cache=cache
    )
    result = tighten(tt, fc, accepted, cache)
    assert tt.var_train[result[0][0][0].variant] == 100


@pytest.mark.parametrize("delay,count", [(4, 1), (0, 0)])
def test_late_instance(delay, count):
    tt = make_late_connection_timetable()
    fc = FixedForecaster(tt, {(100, 2): (4,) * 3, (250, 2): (delay,) * 3})
    spec = parse_route_key(tt, "pl100@1>2~pl250@2>4", 1, 4)
    assert (
        len(route_instances(tt, fc, spec, epoch(clock="06:00"), epoch(clock="10:00")))
        == count
    )


def departure_times(tt, accepted):
    return [
        epoch_clock(int(tt.stop_dep[int(tt.var_first[legs[0].variant]) + legs[0].board_pos]))
        for legs, _ in accepted
    ]


def epoch_clock(seconds):
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def test_chain_lists_connections_in_the_order_they_leave():
    tt = make_timetable()
    fc = FixedForecaster(tt, {})
    accepted, limited, searches = plan_chain(
        tt, fc, 0, 3, epoch(clock="07:00"), Budget(), cache={}
    )
    # 102 (07:30) is beaten by 100 (08:00), which arrives no later; then the ICE; then nothing
    # else leaves in time to arrive within the search horizon.
    assert departure_times(tt, accepted) == ["08:00", "08:10"]
    assert [k for _, k in accepted] == ["pl100@1>2~pl200@2>4", "cICE@1>4"]
    assert not limited and searches >= 2


def test_chain_stops_at_the_departure_cap_and_keeps_the_limit():
    tt = make_timetable()
    fc = FixedForecaster(tt, {})
    accepted, _, _ = plan_chain(
        tt, fc, 0, 3, epoch(clock="07:00"), Budget(), cache={}, depart_until=epoch(clock="08:05")
    )
    assert departure_times(tt, accepted) == ["08:00"]
    accepted, _, _ = plan_chain(
        tt, fc, 0, 3, epoch(clock="07:00"), Budget(), cache={}, limit=1
    )
    assert departure_times(tt, accepted) == ["08:00"]


def test_chain_before_keeps_the_last_connections_before_the_time():
    tt = make_timetable()
    fc = FixedForecaster(tt, {})
    accepted, _, _ = plan_chain_before(
        tt, fc, 0, 3, epoch(clock="08:10"), Budget(), cache={}
    )
    assert departure_times(tt, accepted) == ["08:00"]
    accepted, _, _ = plan_chain_before(
        tt, fc, 0, 3, epoch(clock="08:11"), Budget(), cache={}, limit=5
    )
    assert departure_times(tt, accepted) == ["08:00", "08:10"]
    nothing, _, _ = plan_chain_before(
        tt, fc, 0, 3, epoch(clock="07:30"), Budget(), cache={}
    )
    assert nothing == []


def test_chain_before_leaves_out_what_the_connection_after_it_beats():
    tt = make_timetable()
    fc = FixedForecaster(tt, {})
    # Before 08:10 there are 102 (07:30, via B) and 100 (08:00, via B): 100 arrives as soon and
    # leaves later, so only it is kept; the ICE at 08:10 itself is the one after.
    accepted, _, _ = plan_chain_before(
        tt, fc, 0, 3, epoch(clock="08:10"), Budget(), cache={}, limit=5
    )
    assert departure_times(tt, accepted) == ["08:00"]
