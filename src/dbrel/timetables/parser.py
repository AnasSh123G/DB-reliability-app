"""IRIS-TTS timetable XML -> plain dataclasses.

Pure: takes bytes, returns data, touches no database. Mirrors realtime/parser.py,
including the `keep` predicate and the `value()` tuple that change detection
compares against the last stored observation.

Everything here is grounded in docs/probe-findings.md §10-§17.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from xml.etree import ElementTree as ET

from dbrel.config import TZ

# Event status (ps/cs) and the trip type (tl@t) enums.
STATUS_PLANNED = "p"
STATUS_ADDED = "a"
STATUS_CANCELLED = "c"

# Categories that carry no dp@l line indicator: identity is the number alone.
LONG_DISTANCE_CATEGORIES = frozenset({"ICE", "IC", "EC", "ECE", "NJ", "EN", "RJ", "TGV", "FLX"})


def parse_ts(value: str | None) -> int | None:
    """'2609110646' (YYMMddHHmm, Europe/Berlin wall clock) -> epoch seconds.

    No offset is transmitted, so the ambiguous hour of the autumn DST change maps
    to fold=0 here. plan_import fixes non-monotonic runs afterwards; a single
    timestamp in isolation cannot be disambiguated.
    """
    if not value or len(value) != 10 or not value.isdigit():
        return None
    moment = datetime(
        2000 + int(value[0:2]),
        int(value[2:4]),
        int(value[4:6]),
        int(value[6:8]),
        int(value[8:10]),
        tzinfo=TZ,
    )
    return int(moment.timestamp())


def split_stop_id(stop_id: str) -> tuple[str, str, int] | None:
    """'-7874...321-2609110526-6' -> ('-7874...321', '2609110526', 6).

    rsplit, never split: the daily trip id is a signed 64-bit integer and is
    frequently negative, so the leading '-' belongs to it (findings §12).
    """
    try:
        daily, stamp, index = stop_id.rsplit("-", 2)
    except ValueError:
        return None
    if not index.isdigit() or len(stamp) != 10 or not stamp.isdigit():
        return None
    return daily, stamp, int(index)


def run_key(stop_id: str) -> str | None:
    """The per-day identity of a run, identical at every station it calls at.

    Dropping the stop index is what makes one physical run seen at Bochum and at
    Dortmund resolve to a single trip row.
    """
    parsed = split_stop_id(stop_id)
    return f"{parsed[0]}-{parsed[1]}" if parsed else None


def service_date_of(stamp: str) -> str:
    """'2609110526' -> '2026-09-11'. The planned departure date at the ORIGIN."""
    return f"20{stamp[0:2]}-{stamp[2:4]}-{stamp[4:6]}"


@dataclass(slots=True)
class TtEvent:
    """One side (arrival or departure) of a stop."""

    planned_ts: int | None
    changed_ts: int | None
    planned_platform: str | None
    changed_platform: str | None
    planned_status: str | None
    changed_status: str | None
    planned_path: str | None
    changed_path: str | None
    line: str | None
    cancelled_at: int | None
    hidden: bool
    wings: str | None
    transition: str | None

    @property
    def delay_s(self) -> int | None:
        """IRIS publishes no delay field; it is ct - pt (findings §16)."""
        if self.planned_ts is None or self.changed_ts is None:
            return None
        return self.changed_ts - self.planned_ts

    @property
    def is_cancelled(self) -> bool:
        return self.changed_status == STATUS_CANCELLED

    @property
    def is_added(self) -> bool:
        return self.changed_status == STATUS_ADDED or self.planned_status == STATUS_ADDED

    def path_names(self) -> list[str]:
        raw = self.changed_path or self.planned_path
        return [n.strip() for n in raw.split("|") if n.strip()] if raw else []


@dataclass(slots=True)
class TtMessage:
    msg_id: str | None
    msg_type: str | None
    code: int | None
    category: str | None
    ts: int | None
    valid_from: int | None
    valid_to: int | None
    priority: int | None
    deleted: bool


@dataclass(slots=True)
class TtStop:
    stop_id: str
    eva: int | None
    daily_trip_id: str
    start_stamp: str
    stop_index: int
    category: str | None
    number: str | None
    owner: str | None
    trip_type: str | None
    filter_flags: str | None
    label: str | None  # the undocumented fb attribute, e.g. "ICE 843"
    arrival: TtEvent | None
    departure: TtEvent | None
    messages: list[TtMessage] = field(default_factory=list)

    @property
    def run_key(self) -> str:
        return f"{self.daily_trip_id}-{self.start_stamp}"

    @property
    def service_date(self) -> str:
        return service_date_of(self.start_stamp)

    @property
    def line(self) -> str | None:
        for event in (self.departure, self.arrival):
            if event and event.line:
                return event.line
        return None

    @property
    def is_long_distance(self) -> bool:
        return self.line is None or (self.category or "") in LONG_DISTANCE_CATEGORIES

    def full_path(self, station_name: str) -> list[str]:
        """The complete calling sequence, recoverable from one sighting.

        ar@ppth holds every stop before this one, dp@ppth every stop after, so the
        whole route is available even at a station set of one (findings §12).
        """
        before = self.arrival.path_names() if self.arrival else []
        after = self.departure.path_names() if self.departure else []
        return [*before, station_name, *after]

    def value(self) -> tuple:
        """The tuple change detection compares against the last stored observation."""

        def side(event: TtEvent | None) -> tuple:
            if event is None:
                return (None, None, None, None)
            return (event.changed_ts, event.changed_platform, event.changed_status, event.cancelled_at)

        return side(self.arrival) + side(self.departure)


@dataclass(slots=True)
class TtTimetable:
    eva: int | None
    station: str | None
    stops: list[TtStop]
    parse_ms: int
    n_raw_stops: int


def _event(elem: ET.Element | None) -> TtEvent | None:
    if elem is None:
        return None
    get = elem.get
    return TtEvent(
        planned_ts=parse_ts(get("pt")),
        changed_ts=parse_ts(get("ct")),
        planned_platform=get("pp"),
        changed_platform=get("cp"),
        planned_status=get("ps"),
        changed_status=get("cs"),
        planned_path=get("ppth"),
        changed_path=get("cpth"),
        line=get("l"),
        cancelled_at=parse_ts(get("clt")),
        hidden=get("hi") == "1",
        wings=get("wings"),
        transition=get("tra"),
    )


def _messages(elem: ET.Element) -> list[TtMessage]:
    out = []
    for m in elem.findall("m"):
        code = m.get("c")
        priority = m.get("pr")
        out.append(
            TtMessage(
                msg_id=m.get("id"),
                msg_type=m.get("t"),
                code=int(code) if code and code.lstrip("-").isdigit() else None,
                category=m.get("cat"),
                ts=parse_ts(m.get("ts")),
                valid_from=parse_ts(m.get("from")),
                valid_to=parse_ts(m.get("to")),
                priority=int(priority) if priority and priority.isdigit() else None,
                deleted=m.get("del") == "1",
            )
        )
    return out


def parse_timetable(data: bytes, keep: Callable[[str], bool] | None = None) -> TtTimetable:
    """Parse a /plan or /fchg response.

    `keep` is offered the run key and filters before any stop object is built —
    the same discipline as the GTFS-RT parser, and load-bearing for /fchg, which
    returns every change over a many-hour horizon on every poll.
    """
    started = time.monotonic()
    root = ET.fromstring(data)
    eva_attr = root.get("eva")
    stops: list[TtStop] = []
    n_raw = 0

    for elem in root.findall("s"):
        n_raw += 1
        stop_id = elem.get("id") or ""
        parsed = split_stop_id(stop_id)
        if parsed is None:
            continue
        daily, stamp, index = parsed
        if keep is not None and not keep(f"{daily}-{stamp}"):
            continue

        tl = elem.find("tl")
        arrival = _event(elem.find("ar"))
        departure = _event(elem.find("dp"))
        label = None
        for event_elem in (elem.find("ar"), elem.find("dp")):
            if event_elem is not None and event_elem.get("fb"):
                label = event_elem.get("fb")
                break
        eva_raw = elem.get("eva") or eva_attr

        stops.append(
            TtStop(
                stop_id=stop_id,
                eva=int(eva_raw) if eva_raw and eva_raw.isdigit() else None,
                daily_trip_id=daily,
                start_stamp=stamp,
                stop_index=index,
                category=tl.get("c") if tl is not None else None,
                number=tl.get("n") if tl is not None else None,
                owner=tl.get("o") if tl is not None else None,
                trip_type=tl.get("t") if tl is not None else None,
                filter_flags=tl.get("f") if tl is not None else None,
                label=label,
                arrival=arrival,
                departure=departure,
                messages=_messages(elem),
            )
        )

    return TtTimetable(
        eva=int(eva_attr) if eva_attr and eva_attr.isdigit() else None,
        station=root.get("station"),
        stops=stops,
        parse_ms=int((time.monotonic() - started) * 1000),
        n_raw_stops=n_raw,
    )


def parse_stations(data: bytes) -> list[dict]:
    """Parse a /station response into dicts. Callers must filter by eva range."""
    root = ET.fromstring(data)
    out = []
    for node in root.findall("station"):
        eva = node.get("eva")
        out.append(
            {
                "eva": int(eva) if eva and eva.isdigit() else None,
                "name": node.get("name"),
                "ds100": node.get("ds100"),
                "meta": node.get("meta"),
                "platforms": node.get("p"),
            }
        )
    return out
