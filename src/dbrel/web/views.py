"""Two pages and two JSON endpoints.

Every view opens read_scope(), which cannot commit. The station page is rendered
server-side so it works with JavaScript off and shows no loading flash; the same
payload is available as JSON so the page can refresh itself in place.
"""

from __future__ import annotations

from django.http import Http404, JsonResponse
from django.shortcuts import render

from dbrel.config import AVG_MIN_SAMPLES, BOARD_BACK_S, BOARD_FWD_S
from dbrel.db import read_scope
from dbrel.models import BASIS_SYNTH
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
    with read_scope() as session:
        stations = queries.tracked_stations(session)
    return render(request, "dbrel/search.html", {"stations": stations})


def suggest(request):
    term = request.GET.get("q", "")
    with read_scope() as session:
        results = queries.suggest(session, term, limit=8)
    return JsonResponse({"query": term, "results": results})


def station(request, station_id: int):
    back_s, fwd_s = _window(request)
    with read_scope() as session:
        board = queries.station_board(
            session,
            station_id,
            basis=request.GET.get("basis"),
            back_s=back_s,
            fwd_s=fwd_s,
        )
    if board is None:
        raise Http404(f"no station {station_id}")
    return render(
        request,
        "dbrel/station.html",
        {
            "board": board,
            "simulated": board["basis"] == BASIS_SYNTH,
            "min_samples": AVG_MIN_SAMPLES,
        },
    )


def train(request, train_id: int):
    with read_scope() as session:
        route = queries.train_route(session, train_id, basis=request.GET.get("basis"))
    if route is None:
        raise Http404(f"no train {train_id}")
    return render(
        request,
        "dbrel/train.html",
        {
            "route": route,
            "simulated": route["basis"] == BASIS_SYNTH,
            "min_samples": AVG_MIN_SAMPLES,
        },
    )


def train_route(request, train_id: int):
    with read_scope() as session:
        route = queries.train_route(session, train_id, basis=request.GET.get("basis"))
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
        board = queries.station_board(
            session,
            station_id,
            basis=request.GET.get("basis"),
            back_s=back_s,
            fwd_s=fwd_s,
        )
    if board is None:
        return JsonResponse({"error": f"no station {station_id}"}, status=404)
    return JsonResponse(board)
