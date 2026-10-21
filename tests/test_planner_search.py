from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from planner_fixture import make_timetable, make_late_connection_timetable
from dbrel.planner.forecast import FixedForecaster
from dbrel.planner.search import Budget, Ban, earliest_arrival

TZ = ZoneInfo("Europe/Berlin")


def epoch(day="2026-10-19", clock="08:00"):
    return int(datetime.fromisoformat(day + "T" + clock).replace(tzinfo=TZ).timestamp())


def run(
    tt=None,
    delays=None,
    day="2026-10-19",
    clock="08:00",
    source=1,
    target=4,
    budget=None,
    bans=frozenset(),
):
    tt = tt or make_timetable()
    fc = FixedForecaster(tt, delays or {})
    cache = {}
    res = earliest_arrival(
        tt,
        fc,
        tt.si_of_id[source],
        tt.si_of_id[target],
        epoch(day, clock),
        bans,
        budget or Budget(),
        cache,
    )
    return tt, res, cache


def trains(tt, res):
    return [int(tt.var_train[l.variant]) for l in res.legs] if res.legs else None


@pytest.mark.parametrize(
    "kwargs,want",
    [
        ({}, [100, 200]),
        ({"delays": {(100, 2): (4, 4, 4)}}, [300]),
        ({"day": "2026-10-10"}, [700]),
        ({"day": "2026-10-03"}, [100, 200]),
        ({"clock": "10:00", "target": 3}, None),
        ({"clock": "10:00", "target": 2}, [500]),
        ({"day": "2026-10-20", "clock": "00:10", "source": 2}, [600]),
        ({"delays": {(200, 4): (-2, -2, -2)}}, [100, 200]),
        ({"delays": {(200, 4): (float("nan"),) * 3}}, [100, 200]),
    ],
)
def test_search(kwargs, want):
    tt, res, _ = run(**kwargs)
    assert trains(tt, res) == want
    assert not res.hit_limit


def test_sunday_and_ban():
    tt, res, _ = run(day="2026-10-18")
    assert 300 not in trains(tt, res)
    tt = make_timetable()
    _, res, _ = run(tt=tt, bans={Ban("pl100", tt.si_of_id[1])})
    assert trains(tt, res) == [300]


def test_unreachable_target_is_settled_without_forecasting_anything():
    tt, res, cache = run(target=9)
    assert res.legs is None and not res.hit_limit
    assert res.runs_forecast == 0 and cache == {}


def test_departure_cap_keeps_the_first_train_before_it():
    tt = make_timetable()
    fc = FixedForecaster(tt, {})
    found = {}
    for until in ("08:09", "07:59"):
        result = earliest_arrival(
            tt,
            fc,
            tt.si_of_id[1],
            tt.si_of_id[4],
            epoch(clock="06:00"),
            frozenset(),
            Budget(),
            {},
            depart_until=epoch(clock=until),
        )
        found[until] = trains(tt, result)
    # Until 08:09 the 08:00 train 100 still leaves in time; until 07:59 only 102 (07:30) does.
    assert found == {"08:09": [100, 200], "07:59": [102, 200]}


def test_budget():
    _, res, _ = run(budget=Budget(max_runs_per_search=1))
    assert res.hit_limit and res.legs is None


@pytest.mark.parametrize("connecting,want", [(4, [100, 250]), (0, None), (1, None)])
def test_carried_connection(connecting, want):
    tt, res, _ = run(
        tt=make_late_connection_timetable(),
        delays={(100, 2): (4,) * 3, (250, 2): (connecting,) * 3},
    )
    assert trains(tt, res) == want
    assert not res.hit_limit


@pytest.mark.parametrize(
    "kwargs,clock",
    [
        ({}, "09:20"),
        ({"delays": {(100, 2): (4,) * 3}}, "09:30"),
        ({"clock": "10:00", "target": 2}, "10:30"),
        ({"day": "2026-10-20", "clock": "00:10", "source": 2}, "01:00"),
    ],
)
def test_forecast_arrival_and_scheduled_departure(kwargs, clock):
    from dbrel.planner.routes import arrival_epoch, departure_epoch

    tt, res, cache = run(**kwargs)
    assert arrival_epoch(tt, cache, res.legs[-1]) == epoch(
        kwargs.get("day", "2026-10-19"), clock
    )
    if not kwargs:
        assert departure_epoch(tt, res.legs[0]) == epoch()


def test_forecast_batches_and_uncapped_arrival():
    from dbrel.planner.routes import arrival_epoch

    tt = make_timetable()
    fc = FixedForecaster(tt, {(200, 4): (45,) * 3})
    cache = {}
    result = earliest_arrival(tt, fc, 0, 3, epoch(), frozenset(), Budget(), cache)
    assert fc.runs_forecast > fc.calls
    assert trains(tt, result) == [300]
    v = next(i for i, t in enumerate(tt.var_train) if t == 200)
    from dbrel.planner.search import LegRef

    fc_result = fc.forecast([(v, datetime(2026, 10, 19).date())])
    cache.update(fc_result)
    assert arrival_epoch(
        tt, cache, LegRef(v, datetime(2026, 10, 19).date(), 0, 1)
    ) == epoch(clock="10:05")


def test_later_arrival_can_reach_departure_beyond_earlier_maxwait():
    from planner_fixture import _build

    tt = _build(
        [
            (10, "pl10", range(7), [(1, None, "08:00"), (2, "08:30", None)]),
            (11, "pl11", range(7), [(1, None, "09:00"), (2, "09:30", None)]),
            (20, "pl20", range(7), [(2, None, "12:00"), (4, "13:00", None)]),
        ]
    )
    tt, result, _ = run(tt=tt)
    assert trains(tt, result) == [11, 20]
    assert not result.hit_limit


def test_later_fewer_leg_arrival_preserves_remaining_leg_allowance(monkeypatch):
    import planner_fixture
    from planner_fixture import _build

    monkeypatch.setattr(
        planner_fixture,
        "STATIONS",
        planner_fixture.STATIONS
        + [
            (5, "E", 51.0, 8.1, "DE-NW"),
            (6, "F", 51.0, 8.2, "DE-NW"),
            (7, "G", 51.0, 8.3, "DE-NW"),
        ],
    )
    tt = _build(
        [
            (10, "pl10", range(7), [(1, None, "08:00"), (2, "08:30", None)]),
            (11, "pl11", range(7), [(2, None, "08:35"), (3, "09:00", None)]),
            (12, "pl12", range(7), [(3, None, "09:05"), (5, "09:30", None)]),
            (13, "pl13", range(7), [(5, None, "09:35"), (6, "10:00", None)]),
            (14, "pl14", range(7), [(1, None, "09:30"), (6, "10:10", None)]),
            (20, "pl20", range(7), [(6, None, "10:20"), (7, "11:00", None)]),
            (21, "pl21", range(7), [(7, None, "11:05"), (4, "12:00", None)]),
        ]
    )
    tt, result, _ = run(tt=tt)
    assert trains(tt, result) == [14, 20, 21]
    assert not result.hit_limit


def test_equal_arrival_prefers_later_first_departure_when_boarding_same_run_downstream():
    from planner_fixture import _build

    tt = _build(
        [
            (10, "pl10", range(7), [(1, None, "08:00"), (2, "08:30", None)]),
            (11, "pl11", range(7), [(1, None, "09:00"), (3, "09:30", None)]),
            (
                20,
                "pl20",
                range(7),
                [(2, None, "10:00"), (3, "10:30", "10:31"), (4, "11:00", None)],
            ),
        ]
    )
    tt, result, _ = run(tt=tt)
    assert trains(tt, result) == [11, 20]


def test_revisiting_source_keeps_transfer_wait_limit():
    from planner_fixture import _build

    tt = _build(
        [
            (10, "pl10", range(7), [(1, None, "08:00"), (2, "08:30", None)]),
            (11, "pl11", range(7), [(2, None, "11:00"), (1, "11:30", None)]),
            (20, "pl20", range(7), [(1, None, "15:00"), (4, "16:00", None)]),
        ]
    )
    _, result, _ = run(tt=tt)
    assert result.legs is None
    assert not result.hit_limit


def test_equal_arrival_prefers_fewer_legs_before_later_departure():
    from planner_fixture import _build

    tt = _build(
        [
            (10, "pl10", range(7), [(1, None, "08:00"), (4, "10:00", None)]),
            (11, "pl11", range(7), [(1, None, "08:30"), (2, "09:00", None)]),
            (20, "pl20", range(7), [(2, None, "09:05"), (4, "10:00", None)]),
        ]
    )
    tt, result, _ = run(tt=tt)
    assert trains(tt, result) == [10]


def test_cached_suffix_bounds_equal_reference_for_every_board_and_date():
    from datetime import date
    from dbrel.planner.search import _SearchCache, heuristic, lower_bound
    from dbrel.planner.timetable import Departure
    from dbrel.ingest.timeutil import service_date_anchor

    tt = make_timetable()
    for target in range(len(tt.station_id)):
        h = heuristic(tt, target)
        local = _SearchCache(tt, h)
        for variant in range(len(tt.var_len)):
            base = int(tt.var_first[variant])
            for end in range(1, int(tt.var_len[variant]) + 1):
                for position in range(end):
                    for day in (
                        date(2026, 10, 19),
                        date(2026, 10, 25),
                        date(2026, 10, 26),
                    ):
                        dep = Departure(
                            variant,
                            day,
                            position,
                            service_date_anchor(day)
                            + int(tt.stop_dep[base + position]),
                        )
                        assert local.lower_bound(dep, end) == lower_bound(
                            tt, dep, end, h
                        )
                        assert local.lower_bound(dep, end) == lower_bound(
                            tt, dep, end, h
                        )


def test_departure_cache_reuses_objects_across_overlapping_windows():
    from dbrel.planner.search import _SearchCache, heuristic

    tt = make_timetable()
    cache = _SearchCache(tt, heuristic(tt, 3))
    lo, hi = epoch(clock="07:00"), epoch(clock="09:00")
    first = cache.departures(0, lo, hi)
    assert list(first) == tt.departures(0, lo, hi)
    cache.departures(1, lo, hi)
    narrow = cache.departures(0, lo + 1, hi - 1)
    again = cache.departures(0, lo, hi)
    assert first and narrow
    assert all(a is b for a, b in zip(first, again, strict=True))
    assert all(any(a is b for b in first) for a in narrow)


@pytest.mark.parametrize(
    "day",
    [
        "2026-03-28",
        "2026-03-29",
        "2026-10-03",
        "2026-10-10",
        "2026-10-18",
        "2026-10-19",
        "2026-10-20",
        "2026-10-24",
        "2026-10-25",
        "2026-10-26",
    ],
)
def test_indexed_departures_equal_public_api_through_holidays_dst_midnight(day):
    from dbrel.planner.search import _SearchCache, heuristic

    tt = make_timetable()
    cache = _SearchCache(tt, heuristic(tt, 3))
    start = epoch(day, "00:00")
    windows = [
        (start, start + 2 * 3600),
        (start + 6 * 3600, start + 10 * 3600),
        (start + 23 * 3600, start + 26 * 3600),
        (start - 3600, start + 49 * 3600),
    ]
    for lo, hi in windows + list(reversed(windows)):
        for si in range(len(tt.station_id)):
            assert list(cache.departures(si, lo, hi)) == tt.departures(si, lo, hi)
    for si in range(len(tt.station_id)):
        for dep in tt.departures(si, start, start + 26 * 3600):
            assert list(
                cache.departures(si, dep.dep_epoch, dep.dep_epoch)
            ) == tt.departures(si, dep.dep_epoch, dep.dep_epoch)


def test_search_records_use_slots():
    from dbrel.planner.search import Ban, LegRef, Label, RunScan
    from datetime import date

    day = date(2026, 10, 19)
    state = (0, 1, 100, 0)
    records = [
        Ban("cICE", 0),
        LegRef(0, day, 0, 1),
        Label(100, 1, None, None, 0),
        RunScan((0, day), 0, 0, 1, 100, False, state, 0),
    ]
    assert all(not hasattr(record, "__dict__") for record in records)


def test_already_feasible_run_is_pruned_before_second_enqueue(monkeypatch):
    from planner_fixture import _build
    import dbrel.planner.search as module

    tt = _build(
        [
            (
                10,
                "pl10",
                range(7),
                [(1, None, "08:00"), (2, "08:30", "08:31"), (3, "10:20", None)],
            ),
            (
                20,
                "pl20",
                range(7),
                [(2, None, "09:00"), (3, "10:00", "10:01"), (4, "11:00", None)],
            ),
        ]
    )
    bounds = []
    original = module._SearchCache.lower_bound

    def bound(self, departure, end):
        bounds.append((int(tt.var_train[departure.variant]), departure.position))
        return original(self, departure, end)

    monkeypatch.setattr(module._SearchCache, "lower_bound", bound)
    tt, result, _ = run(tt=tt, delays={(20, 3): (30, 30, 30)})
    assert trains(tt, result) == [10, 20]
    assert (20, 0) in bounds
    assert (20, 1) not in bounds


@pytest.mark.parametrize(
    "bans,expected", [(frozenset(), [11, 20]), (frozenset({Ban("pl11", 0)}), [10, 20])]
)
def test_later_first_departure_dominates_identical_arrival_state(
    monkeypatch, bans, expected
):
    from planner_fixture import _build
    import dbrel.planner.search as module

    tt = _build(
        [
            (10, "pl10", range(7), [(1, None, "08:00"), (2, "09:00", None)]),
            (11, "pl11", range(7), [(1, None, "08:30"), (2, "09:00", None)]),
            (20, "pl20", range(7), [(2, None, "09:05"), (4, "10:00", None)]),
        ]
    )
    station_expansions = []
    original = module._SearchCache.departures

    def departures(self, si, lo, hi):
        station_expansions.append(si)
        return original(self, si, lo, hi)

    monkeypatch.setattr(module._SearchCache, "departures", departures)
    tt, result, _ = run(tt=tt, bans=bans)
    assert trains(tt, result) == expected
    assert station_expansions.count(tt.si_of_id[2]) == 1


@pytest.mark.parametrize(
    "bans,expected",
    [
        (frozenset(), [10, 20]),
        (frozenset({Ban("pl10", 0)}), [11, 12, 20]),
    ],
)
def test_fewer_legs_dominate_same_arrival_even_with_earlier_departure(
    monkeypatch, bans, expected
):
    from planner_fixture import _build
    import dbrel.planner.search as module

    tt = _build(
        [
            (10, "pl10", range(7), [(1, None, "08:00"), (2, "09:00", None)]),
            (11, "pl11", range(7), [(1, None, "08:20"), (3, "08:40", None)]),
            (12, "pl12", range(7), [(3, None, "08:45"), (2, "09:00", None)]),
            (20, "pl20", range(7), [(2, None, "09:05"), (4, "10:00", None)]),
        ]
    )
    expansions = []
    original = module._SearchCache.departures

    def departures(self, si, lo, hi):
        expansions.append(si)
        return original(self, si, lo, hi)

    monkeypatch.setattr(module._SearchCache, "departures", departures)
    tt, result, _ = run(tt=tt, bans=bans)
    assert trains(tt, result) == expected
    assert expansions.count(tt.si_of_id[2]) == 1


def test_initial_source_does_not_dominate_exact_time_return_with_transfer_lookback():
    from planner_fixture import _build

    tt = _build(
        [
            (10, "pl10", range(7), [(1, None, "08:00"), (2, "08:01", None)]),
            (
                11,
                "pl11",
                range(7),
                [(3, None, "07:30"), (2, "07:40", "07:41"), (1, "08:00", None)],
            ),
            (
                20,
                "pl20",
                range(7),
                [(3, None, "07:00"), (1, "07:49", "07:50"), (4, "09:00", None)],
            ),
        ]
    )
    tt, result, _ = run(tt=tt, delays={(11, 2): (30,) * 3, (20, 1): (30,) * 3})
    assert trains(tt, result) == [10, 11, 20]
    assert not result.hit_limit


def test_pending_run_retains_earliest_arrival_and_immutable_predecessor():
    from datetime import date
    from dbrel.planner.search import RunScan, _PendingRuns

    pending = _PendingRuns()
    day = date(2026, 10, 20)
    later = RunScan((1, day), 2, 3, 2, 1000, False, (3, 2, 1000, 100), 100)
    earlier = RunScan((1, day), 2, 3, 2, 900, False, (3, 2, 900, 100), 100)
    assert pending.offer(later)
    assert pending.offer(earlier)
    assert pending.active(earlier)
    assert not pending.active(later)
    assert not pending.offer(later)
    assert earlier.prev_state == (3, 2, 900, 100)
    assert later.prev_state == (3, 2, 1000, 100)
    # A different boarding row must survive until its own feasibility is known.
    downstream = RunScan((1, day), 3, 4, 2, 1100, False, (4, 2, 1100, 100), 100)
    assert pending.offer(downstream)
    assert pending.active(earlier) and pending.active(downstream)
    pending.discard(earlier)
    assert not pending.active(earlier) and not pending.active(later)
    assert pending.offer(later)
    assert pending.active(later) and not pending.active(earlier)


def test_converging_different_arrivals_queue_same_run_only_once(monkeypatch):
    from planner_fixture import _build
    import dbrel.planner.search as module

    tt = _build(
        [
            (
                10,
                "pl10",
                range(7),
                [(1, None, "08:00"), (2, "08:30", "08:31"), (3, "08:35", None)],
            ),
            (20, "pl20", range(7), [(2, None, "08:40"), (4, "09:30", None)]),
            (21, "pl21", range(7), [(3, None, "08:40"), (4, "09:00", None)]),
            (30, "pl30", range(7), [(4, None, "10:00"), (9, "11:00", None)]),
        ]
    )
    queued = []
    original = module.heappush

    def push(heap, item):
        if item[4] == 1 and tt.var_train[item[5].key[0]] == 30:
            queued.append(item[5])
        return original(heap, item)

    monkeypatch.setattr(module, "heappush", push)
    tt, result, _ = run(tt=tt, target=9)
    assert trains(tt, result) == [10, 21, 30]
    assert len(queued) == 1


def test_infeasible_earlier_board_position_keeps_feasible_downstream_candidate():
    from planner_fixture import _build

    tt = _build(
        [
            (
                10,
                "pl10",
                range(7),
                [(1, None, "08:00"), (2, "08:30", "08:31"), (3, "08:40", None)],
            ),
            (
                20,
                "pl20",
                range(7),
                [(2, None, "08:32"), (3, "08:49", "08:50"), (4, "09:30", None)],
            ),
        ]
    )
    tt, result, _ = run(tt=tt)
    assert trains(tt, result) == [10, 20]
    assert result.legs[-1].board_pos == 1


def test_cached_integer_delays_preserve_scalar_rounding_and_reuse(monkeypatch):
    import numpy as np
    from dbrel.planner.search import _SearchCache, heuristic
    from dbrel.planner.forecast import delay_s, expected_departure
    import dbrel.planner.search as module

    tt = make_timetable()
    schedule = _SearchCache(tt, heuristic(tt, 3))
    values = np.array(
        [np.nan, -2, 0, 0.5 / 60, 1.5 / 60, 2.5 / 60, 4, 30, 45], dtype=np.float32
    )
    arr = np.repeat(values[:, None], 3, axis=1)
    expected = tuple(delay_s(value) for value in values)
    calls = []
    original = module.delay_s

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(module, "delay_s", counted)
    key = (0, datetime(2026, 10, 19).date())
    first = schedule.delays(key, arr)
    assert first == expected
    assert schedule.delays(key, arr) is first
    assert len(calls) == len(values)
    for v in range(len(tt.var_len)):
        base = int(tt.var_first[v])
        n = int(tt.var_len[v])
        run = (v, key[1])
        for value in values:
            forecast = np.full((n, 3), value, dtype=np.float32)
            fresh = _SearchCache(tt, heuristic(tt, 3))
            for pos in range(n):
                assert fresh.expected_departure(
                    run, forecast, 100, base, pos
                ) == expected_departure(tt, forecast, 100, base, pos)
