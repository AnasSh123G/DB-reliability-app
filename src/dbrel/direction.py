"""Which way along a line a train is running.

Shared between `web/queries.py` (the line/station pickers, live board) and
`derive/stop_averages.py` (the averaging key), so there is exactly one
heuristic for "which direction is this run" -- two implementations would
drift, the same reasoning that keeps `replay.py` from reimplementing
`import_plan`'s grading rules.

A `ServiceLine` id is not one physical line. Short names like "RE7" or "S1"
are reused by unrelated regional services all over Germany -- Schleswig-
Holstein's RE7 (Hamburg-Altona <-> Kiel/Flensburg) and Baden-Württemberg's
RE7 (Karlsruhe <-> Basel) share a `service_line` row because nothing upstream
disambiguates short names by region. Picking one nationwide "longest route"
axis and measuring every train's position against it (the original
`canonical_order`/`direction_of` design) silently misclassifies every train
outside whichever single region happens to have the longest route_path --
confirmed on the real database, where line 97 "RE7" split 416/62 instead of
splitting per region. `_connected_components` fixes this by first grouping a
line's trains into their real regional corridors, and only then picking a
canonical axis and a direction *within* each corridor.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dbrel.models import Train

# Two trains count as "the same corridor" once their route_paths share at
# least this many station names. 1 is too weak -- two entirely unrelated
# regional lines often share exactly one interchange hub (a big Hbf) without
# being the same physical corridor at all. 2 is enough to rule that out while
# still connecting a Y-branch's two arms, which necessarily share a multi-
# station trunk (Hamburg-Altona, Elmshorn, Neumünster, ... for RE7's
# Kiel/Flensburg branches).
_SHARED_NAME_THRESHOLD = 2


def _connected_components(trains: list[Train]) -> list[list[Train]]:
    """Partition `trains` into groups that share a real physical corridor.

    Union-find over trains, connected via an inverted index of station name
    -> trains calling there, so only trains that already share at least one
    stop are ever compared -- unrelated trains (no shared station name at
    all) never pay for a set-intersection check.
    """
    n = len(trains)
    if n <= 1:
        return [[t] for t in trains]

    parent = list(range(n))

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    train_names: list[set[str]] = []
    name_index: dict[str, list[int]] = {}
    for i, train in enumerate(trains):
        names = {part for part in (train.route_path or "").split("|") if part}
        train_names.append(names)
        for name in names:
            name_index.setdefault(name, []).append(i)

    checked: set[tuple[int, int]] = set()
    for bucket in name_index.values():
        if len(bucket) < 2:
            continue
        for a_pos in range(len(bucket)):
            i = bucket[a_pos]
            for b_pos in range(a_pos + 1, len(bucket)):
                j = bucket[b_pos]
                if find(i) == find(j):
                    continue
                key = (i, j) if i < j else (j, i)
                if key in checked:
                    continue
                checked.add(key)
                if len(train_names[i] & train_names[j]) >= _SHARED_NAME_THRESHOLD:
                    union(i, j)

    groups: dict[int, list[Train]] = {}
    for i, train in enumerate(trains):
        groups.setdefault(find(i), []).append(train)
    return list(groups.values())


def canonical_order(trains) -> list[str]:
    """The longest route among `trains`, taken as the axis every run travels along.

    Called both on a whole line (backward-compatible whole-line label/order
    use) and, internally, on a single connected component (correct per-
    corridor direction) -- see `train_direction_map`.
    """
    best: list[str] = []
    for train in trains:
        names = [part for part in (train.route_path or "").split("|") if part]
        if len(names) > len(best):
            best = names
    return best


def direction_of(train, index: dict[str, int]) -> int:
    """0 if the run travels the canonical way along the line, 1 if it runs back.

    Endpoints alone cannot answer this. RB33's short workings terminate at
    Mönchengladbach and Herzogenrath rather than at either end of the line, so its
    six origin/destination pairs describe only two directions. Position along the
    longest route does answer it, and the short workings sort into the right half
    without anyone having to enumerate them.

    `index` must be built from the SAME group of trains `train` belongs to
    (its connected component, not necessarily the whole line) -- see
    `train_direction_map`.
    """
    seen = [index[name] for name in (train.route_path or "").split("|") if name in index]
    if len(seen) < 2:
        ends = (index.get(train.origin_name), index.get(train.dest_name))
        seen = [position for position in ends if position is not None]
    if len(seen) < 2 or seen[-1] == seen[0]:
        return 0
    return 0 if seen[-1] > seen[0] else 1


def train_direction_map(trains) -> dict[int, int]:
    """train.id -> 0/1, computed per connected regional component.

    This is the function that actually fixes the cross-region direction bug:
    each component gets its own canonical axis, so a Schleswig-Holstein RE7
    train is never measured against Baden-Württemberg's RE7 route.
    """
    out: dict[int, int] = {}
    for component in _connected_components(list(trains)):
        canonical = canonical_order(component)
        index = {name: position for position, name in enumerate(canonical)}
        for train in component:
            out[train.id] = direction_of(train, index)
    return out


def line_trains_by_direction(
    session: Session, line_id: int, *, limit: int = 300
) -> tuple[list[str], list[list[Train]]]:
    """Every numbered run on a line, split into its two directions of travel.

    Shared by every view that needs "the runs going this way": the line page, the
    direction/station pickers, and the around-this-time lookup all have to agree
    on which train goes which way, so the split lives here once.

    `canonical` (used for the "towards X" label and station ordering) is still
    the single longest route_path across the whole line -- a per-component
    label would need one label per component, which the current single-label
    API doesn't have room for. This is a cosmetic gap for a line whose regions
    don't share a corridor (the label may name a station in a different
    region than the trains it's labelling); the direction VALUE each train
    gets (0/1) is unaffected, since that's computed by `train_direction_map`
    per component, not from this `canonical` list.
    """
    trains = session.execute(
        select(Train)
        .where(Train.line_id == line_id)
        .order_by(Train.first_departure_sec, Train.train_number)
        .limit(limit)
    ).scalars().all()

    canonical = canonical_order(trains)
    direction_map = train_direction_map(trains)
    halves: list[list[Train]] = [[], []]
    for t in trains:
        halves[direction_map[t.id]].append(t)
    return canonical, halves


def direction_by_train_id(session: Session, line_id: int, *, limit: int = 100_000) -> dict[int, int]:
    """train_id -> 0/1 for every train on a line. Used to build a temp table for aggregation.

    Same per-component split as `line_trains_by_direction`, just flattened to
    a lookup instead of two lists, and with a much larger default limit -- a
    full-network averaging pass needs every train on a line, not just the
    ones a UI picker would ever show.
    """
    trains = session.execute(
        select(Train).where(Train.line_id == line_id).limit(limit)
    ).scalars().all()
    return train_direction_map(trains)
