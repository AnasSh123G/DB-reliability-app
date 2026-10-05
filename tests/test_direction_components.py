"""`_connected_components` groups trains exactly as comparing every pair of trains would."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from dbrel.direction import _SHARED_NAME_THRESHOLD, _connected_components


def _reference(trains):
    """The groups by brute force: compare every pair, join those sharing enough names.

    Args:
        trains: objects with a `route_path` of '|'-separated station names.

    Returns:
        Groups as lists of trains, ordered by first member, members in input order.
    """
    names = [{p for p in (t.route_path or "").split("|") if p} for t in trains]
    group_of = list(range(len(trains)))

    def root(x):
        """The representative of `x`'s group."""
        while group_of[x] != x:
            x = group_of[x]
        return x

    for i in range(len(trains)):
        for j in range(i + 1, len(trains)):
            if len(names[i] & names[j]) >= _SHARED_NAME_THRESHOLD:
                group_of[root(i)] = root(j)
    groups = {}
    for i, train in enumerate(trains):
        groups.setdefault(root(i), []).append(train)
    return list(groups.values())


def _trains(seed: int, n: int, stations: int):
    """`n` random trains over `stations` station names: some empty, single-stop, duplicate routes."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        kind = rng.random()
        if kind < 0.05:
            path = None
        elif kind < 0.1:
            path = f"S{rng.randrange(stations)}"
        elif kind < 0.3 and out:
            path = rng.choice(out).route_path  # an exact copy of another train's route
        else:
            start = rng.randrange(stations)
            path = "|".join(f"S{(start + k) % stations}" for k in range(rng.randint(2, 8)))
        out.append(SimpleNamespace(id=i, route_path=path))
    return out


@pytest.mark.parametrize("seed", range(40))
def test_the_groups_and_their_order_match_comparing_every_pair(seed):
    """Random lines (sparse to dense) give exactly the brute-force groups, in the same order."""
    trains = _trains(seed, n=60 + seed * 3, stations=20 + seed)
    got = [[t.id for t in group] for group in _connected_components(trains)]
    want = [[t.id for t in group] for group in _reference(trains)]
    assert got == want


def test_one_shared_hub_does_not_join_two_corridors():
    """Two regional lines meeting at one station stay apart; a shared trunk joins two branches."""
    trains = [SimpleNamespace(id=i, route_path=p) for i, p in enumerate([
        "Hamburg|Elmshorn|Neumünster|Kiel",
        "Hamburg|Elmshorn|Neumünster|Flensburg",
        "Karlsruhe|Offenburg|Basel",
        "Kiel|Eckernförde",  # shares only Kiel with the first: not the same corridor
    ])]
    assert [[t.id for t in g] for g in _connected_components(trains)] == [[0, 1], [2], [3]]
