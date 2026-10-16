"""JSON presentation of planned journeys and change risk."""

from datetime import datetime
import math

from dbrel.config import TZ
from dbrel.ingest.timeutil import service_date_anchor
from dbrel.planner.constants import MIN_TRANSFER_S
from dbrel.planner.forecast import delay_s, expected_departure


def moment(epoch):
    local = datetime.fromtimestamp(int(epoch), TZ)
    return {
        "epoch": int(epoch),
        "date": local.strftime("%Y-%m-%d"),
        "time": local.strftime("%H:%M"),
    }


def forecast_json(arr_row):
    if math.isnan(float(arr_row[1])):
        return None
    return dict(
        zip(
            ("p25_min", "p50_min", "p75_min"),
            (float(x) if math.isfinite(float(x)) else None for x in arr_row),
        )
    )


def _leg_times(tt, cache, leg):
    base = int(tt.var_first[leg.variant])
    anchor = service_date_anchor(leg.service_date)
    arr = cache[(leg.variant, leg.service_date)]
    scheduled = anchor + int(tt.stop_arr[base + leg.alight_pos])
    return base, anchor, arr, scheduled, scheduled + delay_s(arr[leg.alight_pos, 1])


def _station(tt, si):
    return {"id": int(tt.station_id[si]), "name": tt.station_name[si]}


# Chance of missing a change, ordered from safest to worst.
MISS_CHANCES = ("low", "medium", "high", "very_high")


def miss_chance(quantiles, slack_s):
    """Band for the chance that the arrival delay eats more than `slack_s`.

    The model gives the 25th, 50th and 75th percentile of the arrival delay, so the chance of
    missing is placed between them: under 25% when even the 75th percentile fits, 25-50% when
    the median fits, 50-75% when the 25th percentile fits, over 75% otherwise. None without a
    forecast.
    """
    p25, p50, p75 = (float(q) for q in quantiles)
    if math.isnan(p50):
        return None
    if delay_s(p75) <= slack_s:
        return "low"
    if delay_s(p50) <= slack_s:
        return "medium"
    if delay_s(p25) <= slack_s:
        return "high"
    return "very_high"


def change_json(tt, cache, incoming, outgoing):
    base, _, arr, scheduled, expected = _leg_times(tt, cache, incoming)
    next_base = int(tt.var_first[outgoing.variant])
    anchor = service_date_anchor(outgoing.service_date)
    next_arr = cache[(outgoing.variant, outgoing.service_date)]
    departure = anchor + int(tt.stop_dep[next_base + outgoing.board_pos])
    exp_dep = expected_departure(tt, next_arr, anchor, next_base, outgoing.board_pos)
    # Delay the incoming train can have and still leave the minimum time to change.
    slack_s = exp_dep - MIN_TRANSFER_S - scheduled
    chance = miss_chance(arr[incoming.alight_pos], slack_s)
    return {
        "station": _station(tt, int(tt.stop_si[base + incoming.alight_pos])),
        "planned_min": (departure - scheduled) // 60,
        "left_min": (exp_dep - expected) // 60,
        "at_risk": chance not in (None, "low"),
        "miss_chance": chance,
    }


def worst_miss_chance(changes):
    chances = [c["miss_chance"] for c in changes if c["miss_chance"] is not None]
    return max(chances, key=MISS_CHANCES.index) if chances else None


def journey_json(tt, cache, legs, names):
    from dbrel.planner.routes import route_key

    rows = []
    for leg in legs:
        base, anchor, arr, scheduled, expected = _leg_times(tt, cache, leg)
        train_id = int(tt.var_train[leg.variant])
        name, number_name, category = names(train_id)
        rows.append(
            {
                "train_id": train_id,
                "name": name,
                "number_name": number_name,
                "category": category,
                "line_key": tt.var_line_key[leg.variant],
                "from": _station(tt, int(tt.stop_si[base + leg.board_pos])),
                "to": _station(tt, int(tt.stop_si[base + leg.alight_pos])),
                "departure": moment(anchor + int(tt.stop_dep[base + leg.board_pos])),
                "arrival": moment(scheduled),
                "expected_arrival": moment(expected),
                "forecast": forecast_json(arr[leg.alight_pos]),
                "n_stops": int(leg.alight_pos - leg.board_pos),
            }
        )
    changes = [change_json(tt, cache, a, b) for a, b in zip(legs, legs[1:])]
    first, last = rows[0], rows[-1]
    return {
        "route_key": route_key(tt, legs),
        "departure": first["departure"],
        "arrival": last["arrival"],
        "expected_arrival": last["expected_arrival"],
        "delay_min": round(
            (last["expected_arrival"]["epoch"] - last["arrival"]["epoch"]) / 60
        ),
        "forecast": last["forecast"],
        "duration_min": round(
            (last["expected_arrival"]["epoch"] - first["departure"]["epoch"]) / 60
        ),
        "n_changes": len(changes),
        "at_risk": any(c["at_risk"] for c in changes),
        "miss_chance": worst_miss_chance(changes),
        "legs": rows,
        "changes": changes,
    }
