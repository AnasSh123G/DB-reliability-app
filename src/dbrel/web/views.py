"""JSON endpoints, plus a catch-all that proxies to the frontend's own server.

Every JSON view opens read_scope(), which cannot commit. Rendering (search,
station board, train route, line detail) now lives entirely in the
`bahn-buddy-predict` frontend, which `dbrel serve` runs as its own subprocess
(see cli.py) -- `frontend_proxy` is the other half of that: any request the
JSON patterns in urls.py don't match is forwarded to it, so the operator still
sees one command and one port (see design.md, decision 6 of
replace-web-ui-with-bahn-buddy).
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import date, datetime

import httpx
from django.http import FileResponse, HttpResponse, JsonResponse

from dbrel.config import (
    BOARD_BACK_S,
    BOARD_FWD_S,
    FRONTEND_PORT,
    FRONTEND_PROXY_TIMEOUT_S,
    ML_MODELS_DIR,
    TZ,
)
from dbrel.db import read_scope
from dbrel.ml.bakeoff import current_artifact
from dbrel.planner.constants import PLAN_CONCURRENCY, PLAN_QUEUE_SECONDS
from dbrel.web import queries

# Headers that describe THIS hop's transport, not the proxied response's actual
# content -- forwarding them verbatim would mislead the client (e.g. a stale
# content-length after Django's own gzip middleware re-encodes the body).
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}


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


def suggest(request):
    term = request.GET.get("q", "")
    only = request.GET.get("only")
    if only not in (None, "station", "line", "train"):
        return JsonResponse({"error": "only must be station, line or train"}, status=400)
    state = request.GET.get("state") or None  # "" is "all of Germany"
    if state is not None and state not in queries.STATES:
        return JsonResponse({"error": f"state must be one of {', '.join(queries.STATES)}"}, status=400)
    with read_scope() as session:
        results = queries.suggest(session, term, limit=8, only=only, state=state)
    return JsonResponse({"query": term, "results": results})


def train_route(request, train_id: int):
    station = request.GET.get("station", "")
    raw_date = request.GET.get("date", "")
    service_date = None
    if raw_date:
        try:
            service_date = date.fromisoformat(raw_date)
        except ValueError:
            return JsonResponse({"error": "date must be YYYY-MM-DD"}, status=400)
    with read_scope() as session:
        route = queries.train_route(
            session,
            train_id,
            station_id=int(station) if station.isdigit() else None,
            service_date=service_date,
        )
    if route is None:
        return JsonResponse({"error": f"no train {train_id}"}, status=404)
    return JsonResponse(route)


def line_detail(request, line_id: int):
    with read_scope() as session:
        detail = queries.line_detail(session, line_id)
    if detail is None:
        return JsonResponse({"error": f"no line {line_id}"}, status=404)
    return JsonResponse(detail)


def _chosen_moment(request) -> tuple[int | None, str | None]:
    """The moment a board is asked for, from its `date` (YYYY-MM-DD) and `time` (HH:MM).

    Args:
        request: the Django request.

    Returns:
        (epoch, None) for a chosen moment in local time, (None, None) when neither parameter is
        given (the board is then "now"), or (None, message) when the values cannot be used.
    """
    raw_date, raw_time = request.GET.get("date"), request.GET.get("time")
    if not raw_date and not raw_time:
        return None, None
    if not raw_date or not raw_time:
        return None, "date and time must be given together"
    try:
        day = datetime.strptime(raw_date, "%Y-%m-%d")
    except ValueError:
        return None, f"date must be YYYY-MM-DD, got {raw_date!r}"
    try:
        clock = datetime.strptime(raw_time, "%H:%M")
    except ValueError:
        return None, f"time must be HH:MM, got {raw_time!r}"
    # fold=0: on the autumn day the clock hour happens twice, and the first one is meant.
    moment = day.replace(hour=clock.hour, minute=clock.minute, tzinfo=TZ, fold=0)
    return int(moment.timestamp()), None


def station_board(request, station_id: int):
    back_s, fwd_s = _window(request)
    now, problem = _chosen_moment(request)
    if problem:
        return JsonResponse({"error": problem}, status=400)
    with read_scope() as session:
        board = queries.station_board(session, station_id, now=now, back_s=back_s, fwd_s=fwd_s)
    if board is None:
        return JsonResponse({"error": f"no station {station_id}"}, status=404)
    return JsonResponse(board)


def line_directions(request, line_id: int):
    with read_scope() as session:
        result = queries.line_directions(session, line_id)
    if result is None:
        return JsonResponse({"error": f"no line {line_id}"}, status=404)
    return JsonResponse(result)


def corridor_directions(request, train_id: int):
    with read_scope() as session:
        result = queries.corridor_directions(session, train_id)
    if result is None:
        return JsonResponse({"error": f"no line-less train {train_id}"}, status=404)
    return JsonResponse(result)


def _stations_response(fetch, key_id: int, request):
    try:
        direction = int(request.GET.get("direction", ""))
    except ValueError:
        return JsonResponse({"error": "direction must be 0 or 1"}, status=400)
    with read_scope() as session:
        stations = fetch(session, key_id, direction)
    if stations is None:
        return JsonResponse({"error": f"no route {key_id} or direction {direction}"}, status=404)
    return JsonResponse({"stations": stations})


def line_stations(request, line_id: int):
    return _stations_response(queries.line_stations, line_id, request)


def corridor_stations(request, train_id: int):
    return _stations_response(queries.corridor_stations, train_id, request)


def line_trips_around(request, line_id: int):
    return _trips_around_response(queries.trips_around, line_id, request)


def corridor_trips_around(request, train_id: int):
    return _trips_around_response(queries.corridor_trips_around, train_id, request)


MAX_WINDOW_SIDE = 40  # runs asked for on one side of the chosen time, at most


def _window_size(request, name: str, default: int) -> int:
    """How many runs the request wants on one side of the chosen time.

    Args:
        request: the Django request; `name` is a query parameter ("before" or "after").
        name: the parameter to read.
        default: the count when the parameter is missing or not a number.

    Returns:
        The count, kept between 0 and MAX_WINDOW_SIDE.
    """
    try:
        return max(0, min(int(request.GET.get(name, default)), MAX_WINDOW_SIDE))
    except ValueError:
        return default


def _trips_around_response(fetch, key_id: int, request):
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
    n_before, n_after = _window_size(request, "before", 2), _window_size(request, "after", 1)
    # By position instead: `count` runs from `offset` (0 = the first run after the chosen time).
    offset = count = None
    if request.GET.get("offset") is not None:
        try:
            offset = int(request.GET["offset"])
            count = int(request.GET.get("count", ""))
        except ValueError:
            return JsonResponse({"error": "offset and count must be whole numbers"}, status=400)
        count = max(1, min(count, MAX_WINDOW_SIDE))
    with read_scope() as session:
        from dbrel.models import Station

        station = session.get(Station, station_id)
        day_type = queries.resolve_day_type(session, date_str, station=station)
        if day_type is None:
            return JsonResponse({"error": f"unusable date {date_str!r}"}, status=400)
        result = fetch(
            session,
            key_id,
            direction,
            station_id,
            time_of_day_s,
            day_type=day_type,
            service_date=date.fromisoformat(date_str),
            n_before=n_before,
            n_after=n_after,
            offset=offset,
            count=count,
        )
    if result is None:
        return JsonResponse({"error": "no such line, direction or station"}, status=404)
    result["date"] = date_str
    result["day_type"] = day_type
    return JsonResponse(result)


def model_file(request, stamp: str):
    """The current delay model's ONNX weights, cached forever under this stamp's URL.

    `stamp` (the artifact's own timestamp directory name, from
    `queries.current_model()`'s first item) makes the URL change exactly when
    `CURRENT` moves to a different artifact -- an artifact's bytes never change
    once written (bakeoff.py: "a second run never touches the [previous]
    artifact"), so a client can cache this response indefinitely. `stamp` is
    not used to locate the file; the response always reflects whatever
    `CURRENT` points to right now, which is the same stamp any API response
    fetched at the same time would carry.
    """
    artifact = current_artifact(ML_MODELS_DIR)
    if artifact is None:
        return JsonResponse(
            {
                "error": (
                    f"no trained delay model under {ML_MODELS_DIR} -- export the TabM "
                    "model with `data/build/torch-rocm/venv/bin/python "
                    "scripts/export_tabm.py` first"
                )
            },
            status=404,
        )
    manifest = json.loads((artifact / "manifest.json").read_text())
    response = FileResponse(
        open(artifact / manifest["model_file"], "rb"), content_type="application/octet-stream"
    )
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def model_meta(request, stamp: str):
    """How the browser turns the current model's ONNX output into a forecast.

    Kind, output name, sides, point rule and (for a distribution) its bin
    edges -- no vocabulary or statistics, which stay server-side. Cached like
    model_file: an artifact never changes once written.
    """
    from dbrel.ml.arrival import browser_meta

    artifact = current_artifact(ML_MODELS_DIR)
    if artifact is None:
        return JsonResponse({"error": f"no trained delay model under {ML_MODELS_DIR}"}, status=404)
    response = JsonResponse(browser_meta(json.loads((artifact / "manifest.json").read_text())))
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def frontend_proxy(request, path: str = ""):
    """Forward anything the JSON patterns above didn't match to the frontend.

    `dbrel serve` spawns the frontend's own built server as a subprocess on
    `FRONTEND_PORT` (see cli.py); this streams the response back rather than
    redirecting, so the operator's browser only ever talks to one port.
    """
    url = f"http://127.0.0.1:{FRONTEND_PORT}/{path}"
    query_string = request.META.get("QUERY_STRING")
    if query_string:
        url = f"{url}?{query_string}"

    # host/content-length describe THIS request's framing, not the proxied
    # one -- httpx recomputes content-length from `content=` itself, and a
    # stale forwarded value conflicts with that (httpx.LocalProtocolError).
    forward_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
    }
    try:
        upstream = httpx.request(
            request.method,
            url,
            content=request.body,
            headers=forward_headers,
            timeout=FRONTEND_PROXY_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return HttpResponse(
            "The frontend server isn't reachable yet -- give it a moment and reload.",
            status=502,
            content_type="text/plain",
        )

    response = HttpResponse(upstream.content, status=upstream.status_code)
    for key, value in upstream.headers.items():
        if key.lower() not in _HOP_BY_HOP:
            response[key] = value
    return response



_PLAN_SLOTS = threading.BoundedSemaphore(PLAN_CONCURRENCY)


@contextmanager
def _planner_slot():
    acquired = _PLAN_SLOTS.acquire(timeout=PLAN_QUEUE_SECONDS)
    try:
        yield acquired
    finally:
        if acquired:
            _PLAN_SLOTS.release()


def _plan_response(request, timings=False):
    try:
        source, target = int(request.GET.get("from", "")), int(request.GET.get("to", ""))
        if source <= 0 or target <= 0:
            raise ValueError
    except ValueError:
        return JsonResponse({"error": "from and to must be station ids"}, status=400)
    if source == target:
        return JsonResponse({"error": "from and to must be different stations"}, status=400)
    if request.GET.get("date") and not request.GET.get("time"):
        return JsonResponse({"error": "time must be HH:MM"}, status=400)
    epoch, problem = _chosen_moment(request)
    if problem or epoch is None:
        return JsonResponse({"error": problem or "date and time are required"}, status=400)
    # strptime accepts shortened fields; the planner's public format is exact.
    for key, pattern, expected in (("date", r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "YYYY-MM-DD"),
                                   ("time", r"[0-9]{2}:[0-9]{2}", "HH:MM")):
        if not re.fullmatch(pattern, request.GET[key]):
            return JsonResponse({"error": f"{key} must be {expected}"}, status=400)
    with read_scope() as session:
        args = (session, source, target, epoch, request.GET['date'], request.GET['time'])
        if timings:
            status, body = queries.plan_timings(*args, request.GET.get('route', ''), slot=_planner_slot)
        else:
            status, body = queries.plan(*args, slot=_planner_slot)
    return JsonResponse(body, status=status)


def plan(request):
    return _plan_response(request)


def plan_timings(request):
    return _plan_response(request, timings=True)
