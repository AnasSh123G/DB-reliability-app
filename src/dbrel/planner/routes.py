"""Distinct line routes, latest departures and instances at other times."""

from __future__ import annotations
import re
from dbrel.ingest.timeutil import service_date_anchor
from .constants import (
    BEFORE_WINDOWS_S,
    CHAIN_MAX,
    CONNECTIONS,
    MAX_SEARCHES,
    ROUTES_K,
    NO_TIME,
    MIN_TRANSFER_S,
    MAX_DEP_DELAY_S,
    MAX_WAIT_TRANSFER_S,
    TIMINGS_MAX,
    TIMINGS_HALF_WINDOW_S,
)
from .forecast import delay_s, expected_departure
from .search import Ban, Budget, LegRef, earliest_arrival

_KEY = re.compile(
    r"^(pl\d+|sl\d+|c[A-Za-z0-9]+)@\d+>\d+(~(pl\d+|sl\d+|c[A-Za-z0-9]+)@\d+>\d+){0,4}$"
)


def route_key(tt, legs):
    pieces = []
    for leg in legs:
        base = int(tt.var_first[leg.variant])
        board = int(tt.station_id[tt.stop_si[base + leg.board_pos]])
        alight = int(tt.station_id[tt.stop_si[base + leg.alight_pos]])
        pieces.append(f"{tt.var_line_key[leg.variant]}@{board}>{alight}")
    return "~".join(pieces)


def parse_route_key(tt, key, from_id, to_id):
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        return None
    result = []
    previous = from_id
    for piece in key.split("~"):
        line, stations = piece.split("@")
        board, alight = map(int, stations.split(">"))
        if board != previous or board not in tt.si_of_id or alight not in tt.si_of_id:
            return None
        result.append((line, tt.si_of_id[board], tt.si_of_id[alight]))
        previous = alight
    return result if previous == to_id else None


def departure_epoch(tt, leg):
    return service_date_anchor(leg.service_date) + int(
        tt.stop_dep[int(tt.var_first[leg.variant]) + leg.board_pos]
    )


def arrival_epoch(tt, cache, leg):
    row = int(tt.var_first[leg.variant]) + leg.alight_pos
    return (
        service_date_anchor(leg.service_date)
        + int(tt.stop_arr[row])
        + delay_s(cache[(leg.variant, leg.service_date)][leg.alight_pos, 1])
    )


def sort_key(tt, cache, legs):
    return arrival_epoch(tt, cache, legs[-1]), len(legs), -departure_epoch(tt, legs[0])


def plan_routes(
    tt,
    fc,
    source,
    target,
    depart_epoch,
    budget=None,
    *,
    cache=None,
    k=ROUTES_K,
    max_searches=MAX_SEARCHES,
    depart_until=None,
):
    budget = budget or Budget()
    cache = {} if cache is None else cache
    accepted = []
    candidates = {}
    queue = [frozenset()]
    searched = set()
    n = 0
    hit = False
    while len(accepted) < k:
        while queue and n < max_searches and not budget.request_exhausted():
            bans = queue.pop(0)
            if bans in searched:
                continue
            searched.add(bans)
            n += 1
            res = earliest_arrival(
                tt, fc, source, target, depart_epoch, bans, budget, cache, depart_until
            )
            hit |= res.hit_limit
            if res.legs is None:
                continue
            key = route_key(tt, res.legs)
            if key in {key for _, key in accepted}:
                continue
            sk = sort_key(tt, cache, res.legs)
            if key not in candidates:
                candidates[key] = [res.legs, [bans], sk]
            else:
                candidates[key][1].append(bans)
                if sk < candidates[key][2]:
                    candidates[key][0], candidates[key][2] = res.legs, sk
        if not candidates:
            break
        key = min(candidates, key=lambda c: candidates[c][2])
        legs, ban_sets, _ = candidates.pop(key)
        accepted.append((legs, key))
        for bans in ban_sets:
            for leg in legs:
                board = int(tt.stop_si[int(tt.var_first[leg.variant]) + leg.board_pos])
                queue.append(bans | {Ban(tt.var_line_key[leg.variant], board)})
    if budget.request_exhausted() or (
        queue and n >= max_searches and len(accepted) < k
    ):
        hit = True
    return accepted, hit, n


def _matching(tt, spec, lo, hi):
    line, board, alight = spec
    result = []
    for dep in tt.departures(board, lo, hi):
        if tt.var_line_key[dep.variant] != line:
            continue
        base = int(tt.var_first[dep.variant])
        for pos in range(dep.position + 1, int(tt.var_cut[dep.variant])):
            if tt.stop_si[base + pos] == alight and tt.stop_arr[base + pos] != NO_TIME:
                result.append(LegRef(dep.variant, dep.service_date, dep.position, pos))
                break
    return result


def _forecast(fc, legs, cache, budget):
    keys = list(
        dict.fromkeys(
            (leg.variant, leg.service_date)
            for leg in legs
            if (leg.variant, leg.service_date) not in cache
        )
    )
    if keys:
        if budget is not None and budget.request_exhausted():
            return False
        cache.update(fc.forecast(keys))
        if budget is not None:
            budget.charge(len(keys))
    return budget is None or not budget.request_exhausted()


def route_instances(
    tt,
    fc,
    legs_spec,
    lo_epoch,
    hi_epoch,
    *,
    cache=None,
    budget=None,
    max_instances=TIMINGS_MAX,
):
    cache = {} if cache is None else cache
    if not legs_spec:
        return []
    first = _matching(tt, legs_spec[0], lo_epoch, hi_epoch)
    if not _forecast(fc, first, cache, budget):
        return []
    instances = []
    for leg in first:
        legs = [leg]
        cur = arrival_epoch(tt, cache, leg)
        for spec in legs_spec[1:]:
            options = _matching(
                tt,
                spec,
                cur + MIN_TRANSFER_S - MAX_DEP_DELAY_S,
                cur + MAX_WAIT_TRANSFER_S,
            )
            if not _forecast(fc, options, cache, budget):
                return instances[:max_instances]
            feasible = [
                leg
                for leg in options
                if expected_departure(
                    tt,
                    cache[(leg.variant, leg.service_date)],
                    service_date_anchor(leg.service_date),
                    int(tt.var_first[leg.variant]),
                    leg.board_pos,
                )
                >= cur + MIN_TRANSFER_S
            ]
            if not feasible:
                break
            chosen = min(
                feasible,
                key=lambda leg: (
                    arrival_epoch(tt, cache, leg),
                    departure_epoch(tt, leg),
                ),
            )
            legs.append(chosen)
            cur = arrival_epoch(tt, cache, chosen)
        if len(legs) == len(legs_spec):
            instances.append(legs)
    if len(legs_spec) > 1:
        dedup = {}
        for legs in instances:
            key = tuple(legs[1:])
            if key not in dedup or departure_epoch(tt, legs[0]) > departure_epoch(
                tt, dedup[key][0]
            ):
                dedup[key] = legs
        instances = list(dedup.values())
    return sorted(instances, key=lambda legs: departure_epoch(tt, legs[0]))[
        :max_instances
    ]


def tighten(tt, fc, accepted, cache, budget=None, depart_until=None):
    result = []
    for legs, key in accepted:
        spec = [
            (
                tt.var_line_key[leg.variant],
                int(tt.stop_si[int(tt.var_first[leg.variant]) + leg.board_pos]),
                int(tt.stop_si[int(tt.var_first[leg.variant]) + leg.alight_pos]),
            )
            for leg in legs
        ]
        dep = departure_epoch(tt, legs[0])
        end = arrival_epoch(tt, cache, legs[-1])
        hi = dep + TIMINGS_HALF_WINDOW_S
        if depart_until is not None:
            hi = min(hi, depart_until)
        options = route_instances(tt, fc, spec, dep, hi, cache=cache, budget=budget)
        options = [ls for ls in options if arrival_epoch(tt, cache, ls[-1]) <= end]
        result.append(
            (
                max(options, key=lambda ls: departure_epoch(tt, ls[0]))
                if options
                else legs,
                key,
            )
        )
    return sorted(result, key=lambda item: sort_key(tt, cache, item[0]))


def plan_chain(
    tt,
    fc,
    source,
    target,
    depart_epoch,
    budget,
    *,
    cache,
    depart_until=None,
    limit=CONNECTIONS,
):
    """Connections in the order they leave, each the best one leaving after the one before.

    The first is the connection that arrives soonest of those leaving at or after `depart_epoch`
    (as late as it can leave and still arrive then); the next is the best of those leaving a
    minute after it, and so on, so no connection is beaten by one that leaves later and arrives
    earlier. `depart_until` stops the chain at that departure.

    Returns:
        (accepted, hit_limit, searches): `accepted` as `plan_routes` gives it.
    """
    chain, start, hit, searches = [], depart_epoch, False, 0
    while len(chain) < limit and not budget.request_exhausted():
        found, limited, used = plan_routes(
            tt,
            fc,
            source,
            target,
            start,
            budget,
            cache=cache,
            k=1,
            depart_until=depart_until,
        )
        hit |= limited
        searches += used
        if not found:
            break
        best = tighten(tt, fc, found, cache, budget=budget, depart_until=depart_until)[0]
        chain.append(best)
        start = departure_epoch(tt, best[0][0]) + 60
    return chain, hit, searches


def plan_chain_before(
    tt, fc, source, target, until_epoch, budget, *, cache, limit=CONNECTIONS
):
    """The last connections leaving before `until_epoch`, in the order they leave.

    Walks a chain forward through windows that end there, growing from one hour back until it
    holds `limit` links, and keeps its last ones. A connection that arrives no sooner than the
    first one leaving at or after `until_epoch` is left out: that one leaves later and arrives
    as soon, so a page of earlier connections never repeats what the page after it showed.
    """
    later, hit, searches = plan_chain(
        tt, fc, source, target, until_epoch, budget, cache=cache, limit=1
    )
    ceiling = arrival_epoch(tt, cache, later[0][0][-1]) if later else None
    best = []
    for lead in BEFORE_WINDOWS_S:
        chain, limited, used = plan_chain(
            tt,
            fc,
            source,
            target,
            until_epoch - lead,
            budget,
            cache=cache,
            depart_until=until_epoch - 1,
            limit=CHAIN_MAX,
        )
        hit |= limited
        searches += used
        chain = [
            link
            for link in chain
            if ceiling is None or arrival_epoch(tt, cache, link[0][-1]) < ceiling
        ]
        best = max(best, chain, key=len)
        if len(best) >= limit or budget.request_exhausted():
            break
    return best[-limit:], hit, searches
