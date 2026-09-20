"""Two pages and two JSON endpoints.

Every view opens read_scope(), which cannot commit. The station page is rendered
server-side so it works with JavaScript off and shows no loading flash; the same
payload is available as JSON so the page can refresh itself in place.
"""

from __future__ import annotations

from datetime import datetime

from django.http import Http404, JsonResponse
from django.shortcuts import render

from dbrel.config import AVG_MIN_SAMPLES, BOARD_BACK_S, BOARD_FWD_S
from dbrel.db import read_scope
from dbrel.web import queries


def _window(request) -> tuple[int, int]:
    """Minutes back/forward from the query string, clamped to something sane."""

    def minutes(name: str, default_s: int) -> int:
        raw = request.GET.get(name)
        if not raw:
            return default_s
        try:
            return max(0, min(12 * 60, int(raw))) * 60
        except ValueError:
            return default_s

    return minutes("back", BOARD_BACK_S), minutes("fwd", BOARD_FWD_S)


def index(request):
    return render(request, "dbrel/search.html")


def suggest(request):
    term = request.GET.get("q", "")
    only = request.GET.get("only")
    if only not in (None, "station", "line", "train"):
        return JsonResponse({"error": "only must be station, line or train"}, status=400)
    with read_scope() as session:
        results = queries.suggest(session, term, limit=8, only=only)
    return JsonResponse({"query": term, "results": results})


def station(request, station_id: int):
    back_s, fwd_s = _window(request)
    with read_scope() as session:
        board = queries.station_board(session, station_id, back_s=back_s, fwd_s=fwd_s)
    if board is None:
        raise Http404(f"no station {station_id}")
    return render(
        request,
        "dbrel/station.html",
        {
            "board": board,
            "min_samples": AVG_MIN_SAMPLES,
        },
    )


def train(request, train_id: int):
    with read_scope() as session:
        route = queries.train_route(session, train_id)
        if route is None:
            raise Http404(f"no train {train_id}")
        line = route["train"]["line"]
        detail = (
            queries.line_detail(session, line["id"], current_train_id=train_id)
            if line
            else None
        )
    return render(
        request,
        "dbrel/train.html",
        {
            "route": route,
            "detail": detail,
            "min_samples": AVG_MIN_SAMPLES,
        },
    )


def train_route(request, train_id: int):
    with read_scope() as session:
        route = queries.train_route(session, train_id)
    if route is None:
        return JsonResponse({"error": f"no train {train_id}"}, status=404)
    return JsonResponse(route)


def line(request, line_id: int):
    with read_scope() as session:
        detail = queries.line_detail(session, line_id)
    if detail is None:
        raise Http404(f"no line {line_id}")
    return render(request, "dbrel/line.html", {"detail": detail})


def station_board(request, station_id: int):
    back_s, fwd_s = _window(request)
    with read_scope() as session:
        board = queries.station_board(session, station_id, back_s=back_s, fwd_s=fwd_s)
    if board is None:
        return JsonResponse({"error": f"no station {station_id}"}, status=404)
    return JsonResponse(board)


def line_directions(request, line_id: int):
    with read_scope() as session:
        result = queries.line_directions(session, line_id)
    if result is None:
        return JsonResponse({"error": f"no line {line_id}"}, status=404)
    return JsonResponse(result)


def line_stations(request, line_id: int):
    try:
        direction = int(request.GET.get("direction", ""))
    except ValueError:
        return JsonResponse({"error": "direction must be 0 or 1"}, status=400)
    with read_scope() as session:
        stations = queries.line_stations(session, line_id, direction)
    if stations is None:
        return JsonResponse({"error": f"no line {line_id} or direction {direction}"}, status=404)
    return JsonResponse({"stations": stations})


def line_trips_around(request, line_id: int):
    date_str = request.GET.get("date", "")
    try:
        direction = int(request.GET.get("direction", ""))
        station_id = int(request.GET.get("station", ""))
        clock = datetime.strptime(request.GET.get("time", ""), "%H:%M")
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return JsonResponse(
            {
                "error": "need direction (0/1), station (id), date (YYYY-MM-DD) "
                "and time (HH:MM)"
            },
            status=400,
        )
    time_of_day_s = clock.hour * 3600 + clock.minute * 60
    with read_scope() as session:
        day_type = queries.resolve_day_type(session, date_str)
        if day_type is None:
            return JsonResponse({"error": f"unusable date {date_str!r}"}, status=400)
        result = queries.trips_around(
            session, line_id, direction, station_id, time_of_day_s, day_type=day_type
        )
    if result is None:
        return JsonResponse({"error": "no such line, direction or station"}, status=404)
    result["date"] = date_str
    result["day_type"] = day_type
    return JsonResponse(result)
