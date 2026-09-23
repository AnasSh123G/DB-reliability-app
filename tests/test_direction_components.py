"""Per-corridor direction resolution.

A `ServiceLine` id is not one physical line -- unrelated regional services
share a short name ("RE7", "S1", ...) all over Germany. `train_direction_map`
has to split a line's trains into their real regional corridors first
(`_connected_components`) and only then compute a direction per corridor, or
every train outside the single longest route gets silently misclassified
into direction 0 (the historical bug this file guards against).
"""

from __future__ import annotations

from dbrel.direction import _connected_components, train_direction_map
from dbrel.models import Train


def _train(id_, route, origin=None, dest=None) -> Train:
    parts = route.split("|")
    return Train(
        id=id_, identity_key=f"t{id_}", route_path=route,
        origin_name=origin or parts[0], dest_name=dest or parts[-1],
    )


def test_two_unrelated_regions_get_their_own_component_and_axis():
    """Same short line name, zero shared stations -- two separate corridors."""
    region_a = [
        _train(1, "Karlsruhe Hbf|Rastatt|Baden-Baden|Offenburg|Freiburg(Breisgau) Hbf|Basel SBB"),
        _train(2, "Basel SBB|Freiburg(Breisgau) Hbf|Offenburg|Baden-Baden|Rastatt|Karlsruhe Hbf"),
    ]
    region_b = [
        _train(3, "Flensburg|Tarp|Jübek|Rendsburg|Neumünster|Hamburg-Altona"),
        _train(4, "Hamburg-Altona|Neumünster|Rendsburg|Jübek|Tarp|Flensburg"),
    ]
    trains = region_a + region_b
    components = _connected_components(trains)
    assert len(components) == 2
    ids_by_component = {frozenset(t.id for t in c) for c in components}
    assert frozenset({1, 2}) in ids_by_component
    assert frozenset({3, 4}) in ids_by_component

    dmap = train_direction_map(trains)
    # Within each region, the two opposite runs must land in opposite halves --
    # the pre-fix bug put every non-Karlsruhe train in direction 0 regardless.
    assert dmap[1] != dmap[2]
    assert dmap[3] != dmap[4]


def test_y_branch_splits_by_side_not_by_branch():
    """Hamburg <-> {Kiel | Flensburg}: both branches share a long trunk, so they
    are ONE connected component, and the two branches sort into the SAME
    direction as each other when travelling the same way past Hamburg."""
    trunk = "Hamburg-Altona|Pinneberg|Elmshorn|Itzehoe|Neumünster"
    to_flensburg = _train(1, trunk + "|Rendsburg|Jübek|Tarp|Flensburg")
    to_kiel = _train(2, trunk + "|Rendsburg|Kiel Hbf")
    from_flensburg = _train(
        3, "Flensburg|Tarp|Jübek|Rendsburg|Neumünster|Itzehoe|Elmshorn|Pinneberg|Hamburg-Altona"
    )
    from_kiel = _train(4, "Kiel Hbf|Rendsburg|Neumünster|Itzehoe|Elmshorn|Pinneberg|Hamburg-Altona")

    trains = [to_flensburg, to_kiel, from_flensburg, from_kiel]
    components = _connected_components(trains)
    assert len(components) == 1, "the two branches share a long trunk, so one corridor"

    dmap = train_direction_map(trains)
    # Away-from-Hamburg trains agree with each other; towards-Hamburg trains
    # agree with each other; the two groups disagree with each other.
    assert dmap[1] == dmap[2]
    assert dmap[3] == dmap[4]
    assert dmap[1] != dmap[3]


def test_a_single_isolated_train_is_its_own_component():
    trains = [_train(1, "A|B|C")]
    components = _connected_components(trains)
    assert len(components) == 1
    assert len(components[0]) == 1


def test_sharing_exactly_one_station_does_not_merge_components():
    """One shared interchange hub is not enough to call two lines the same
    corridor -- only >=2 shared station names does."""
    region_a = [_train(1, "A|B|Hub|C|D")]
    region_b = [_train(2, "X|Y|Hub|Z")]
    components = _connected_components(region_a + region_b)
    assert len(components) == 2
