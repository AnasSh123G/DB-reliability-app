from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from planner_fixture import make_timetable
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


def test_budget():
    _, res, _ = run(budget=Budget(max_runs_per_search=1))
    assert res.hit_limit and res.legs is None


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


