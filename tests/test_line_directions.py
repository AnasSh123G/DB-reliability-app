"""Splitting a line's runs into its two directions of travel.

The case that makes this non-trivial is the short working. RB33 runs Aachen to
Duisburg, but several of its numbers only cover Duisburg to Mönchengladbach or
Duisburg to Herzogenrath, so grouping on (origin, destination) yields six groups
for what a passenger sees as two directions. The fixture reproduces that shape.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dbrel.db import Base
from dbrel.identity import SOURCE_TT
from dbrel.models import (
    DAY_TYPE_SUNDAY_HOLIDAY,
    DAY_TYPE_WEEKDAY,
    ServiceLine,
    Station,
    Train,
    TrainStopAverage,
)
from dbrel.web import queries

LINE_ID = 33
ROUTE = ["Aachen Hbf", "Herzogenrath", "Mönchengladbach Hbf", "Krefeld Hbf", "Duisburg Hbf"]

# (id, number, route as a slice of ROUTE) -- two full runs and two short workings,
# one of each per direction.
RUNS = [
    (601, 10324, ROUTE),        # Aachen -> Duisburg, the whole line
    (602, 10323, ROUTE[::-1]),  # Duisburg -> Aachen, the whole line back
    (603, 10301, ROUTE[:1:-1]),  # Duisburg -> Mönchengladbach, a short working
    (604, 10300, ROUTE[2:]),    # Mönchengladbach -> Duisburg, the same but back
]


def train_row(train_id: int, number: int, path: list[str]) -> Train:
    return Train(
        id=train_id, source=SOURCE_TT, identity_key=f"tts:RB/{number}",
        category="RB", segment="RV", train_number=number, operator="RRX",
        display_name=f"RB {number}", line_id=LINE_ID,
        origin_name=path[0], dest_name=path[-1],
        n_stops=len(path), route_path="|".join(path),
        first_departure_sec=6 * 3600 + number % 60,
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        s.add(ServiceLine(id=LINE_ID, identity_key="RB/RB33", category="RB", line="RB33",
                          display_name="RB33", search_name="rb33", operator="RRX"))
        for index, name in enumerate(ROUTE, start=1):
            s.add(Station(id=index, natural_key=f"s{index}", name=name,
                          lat=51.0 + index, lon=7.0, tracked=1))
        for train_id, number, path in RUNS:
            s.add(train_row(train_id, number, path))
        s.flush()
        yield s


def halves(session) -> tuple[set[str], set[str]]:
    detail = queries.line_detail(session, LINE_ID)
    return tuple({run["name"] for run in d["runs"]} for d in detail["directions"])


def test_the_two_full_runs_land_in_opposite_halves(session):
    one, other = halves(session)
    assert ("RB 10324" in one) != ("RB 10324" in other)
    assert ("RB 10323" in one) != ("RB 10323" in other)
    assert not ({"RB 10324", "RB 10323"} <= one or {"RB 10324", "RB 10323"} <= other)


def test_a_short_working_joins_the_direction_it_travels(session):
    """The whole point: RB 10301 terminates at neither end of the line.

    Grouped by destination it would be a third direction; grouped by position
    along the line it belongs with the other Duisburg-to-Aachen run.
    """
    one, other = halves(session)
    together = one if "RB 10323" in one else other
    assert "RB 10301" in together
    # ...and the short working going the other way is with the other full run.
    opposite = other if together is one else one
    assert {"RB 10324", "RB 10300"} <= opposite


def test_a_line_has_exactly_two_halves(session):
    assert len(queries.line_detail(session, LINE_ID)["directions"]) == 2


def test_each_half_is_named_for_the_end_it_runs_to(session):
    labels = {d["label"] for d in queries.line_detail(session, LINE_ID)["directions"]}
    assert labels == {"towards Aachen Hbf", "towards Duisburg Hbf"}


def test_short_workings_are_marked_as_partial(session):
    detail = queries.line_detail(session, LINE_ID)
    partial = {
        run["name"] for d in detail["directions"] for run in d["runs"] if run["partial"]
    }
    assert partial == {"RB 10301", "RB 10300"}


def test_every_run_appears_exactly_once(session):
    detail = queries.line_detail(session, LINE_ID)
    names = [run["name"] for d in detail["directions"] for run in d["runs"]]
    assert sorted(names) == ["RB 10300", "RB 10301", "RB 10323", "RB 10324"]
    assert detail["n_runs"] == 4


def test_the_current_run_is_flagged_and_only_it(session):
    detail = queries.line_detail(session, LINE_ID, current_train_id=601)
    flagged = [
        run["name"] for d in detail["directions"] for run in d["runs"] if run["current"]
    ]
    assert flagged == ["RB 10324"]


def add_average(session, train_id: int, station_id: int, *, n_dep: int,
                avg_s: float, punctual: float, day_type: str = DAY_TYPE_WEEKDAY) -> None:
    session.add(TrainStopAverage(
        day_type=day_type, train_id=train_id, station_id=station_id,
        sched_sec=station_id * 600, sched_kind="DEP", sched_dep_sec=station_id * 600,
        line_id=LINE_ID, n_dep=n_dep, n_arr=n_dep, n_runs=n_dep, n_known=n_dep,
        avg_dep_delay_s=avg_s, pct_dep_punctual=punctual,
        computed_at=1, rule_version=1,
    ))
    session.flush()


def run_named(session, name: str, **kwargs) -> dict:
    detail = queries.line_detail(session, LINE_ID, **kwargs)
    return next(r for d in detail["directions"] for r in d["runs"] if r["name"] == name)


def test_a_runs_figure_is_weighted_by_how_much_each_stop_saw(session):
    add_average(session, 601, 1, n_dep=10, avg_s=60.0, punctual=0.9)
    add_average(session, 601, 2, n_dep=20, avg_s=120.0, punctual=0.6)
    # (60*10 + 120*20) / 30 = 100s, not the 90s an unweighted mean would give.
    assert run_named(session, "RB 10324")["avg"]["avg_min"] == 1.7
    assert run_named(session, "RB 10324")["avg"]["punctual_pct"] == 70


def test_a_run_with_no_observations_carries_no_figure(session):
    assert run_named(session, "RB 10323")["avg"] is None


def test_stops_with_no_departures_do_not_drag_the_figure_down(session):
    add_average(session, 601, 1, n_dep=10, avg_s=60.0, punctual=0.9)
    add_average(session, 601, 2, n_dep=0, avg_s=None, punctual=None)
    assert run_named(session, "RB 10324")["avg"]["avg_min"] == 1.0


def test_the_sunday_pool_is_not_mixed_into_the_weekday_figure(session):
    add_average(session, 601, 1, n_dep=10, avg_s=60.0, punctual=0.9)
    add_average(session, 601, 2, n_dep=10, avg_s=600.0, punctual=0.1,
                day_type=DAY_TYPE_SUNDAY_HOLIDAY)
    assert run_named(session, "RB 10324")["avg"]["avg_min"] == 1.0
    sunday = run_named(session, "RB 10324", day_type=DAY_TYPE_SUNDAY_HOLIDAY)
    assert sunday["avg"]["avg_min"] == 10.0


def test_another_lines_averages_do_not_leak_in(session):
    """The line comes from the train, not from the aggregate's own line_id.

    add_average() stamps every row with RB33 whatever train it belongs to, so a
    query trusting that denormalised column would pull this one in.
    """
    session.add(ServiceLine(id=99, identity_key="RB/RB99", category="RB", line="RB99",
                            display_name="RB99", search_name="rb99", operator="RRX"))
    other = train_row(700, 99001, ROUTE)
    other.line_id = 99
    session.add(other)
    session.flush()
    add_average(session, 700, 1, n_dep=10, avg_s=6000.0, punctual=0.0)
    assert run_named(session, "RB 10324")["avg"] is None
