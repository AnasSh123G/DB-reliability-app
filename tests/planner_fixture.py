"""Small in-memory schedules shared by planner tests."""

from datetime import date

import numpy as np

from dbrel.planner.constants import NO_TIME
from dbrel.planner.profile import NOTHING
from dbrel.planner.timetable import PlannerTimetable

STATIONS = [
    (i, name, 51.0, lon, "DE-NW")
    for i, name, lon in [
        (1, "A", 7.0),
        (2, "B", 7.5),
        (3, "C", 8.0),
        (4, "D", 8.5),
        (9, "Z", 9.0),
    ]
]
STATES = "BW BY BE BB HB HH HE MV NI NW RP SL SN ST SH TH".split()


def hm(value):
    h, m = map(int, value.split(":"))
    return h * 3600 + m * 60


def _build(trains):
    rows = [
        (
            tr,
            seq,
            station,
            NO_TIME if arr is None else hm(arr),
            NO_TIME if dep is None else hm(dep),
        )
        for tr, _, _, stops in trains
        for seq, (station, arr, dep) in enumerate(stops, 1)
    ]
    return PlannerTimetable.from_columns(
        stations=STATIONS,
        train_id=np.array([r[0] for r in rows], dtype=np.int64),
        path_code=np.zeros(len(rows), dtype=np.int32),
        path_values=[None],
        stop_sequence=np.array([r[1] for r in rows], dtype=np.int32),
        station_id=np.array([r[2] for r in rows], dtype=np.int64),
        arr=np.array([r[3] for r in rows], dtype=np.int32),
        dep=np.array([r[4] for r in rows], dtype=np.int32),
        chosen={
            tr: [None if k in days else NOTHING for k in range(7)]
            for tr, _, days, _ in trains
        },
        line_key_of_train={tr: line for tr, line, _, _ in trains},
        holiday_set={("2026-10-03", "DE-" + state) for state in STATES},
        first_date=date(2026, 9, 22),
        last_date=date(2026, 10, 19),
    )


def make_timetable():
    daily = range(7)
    return _build(
        [
            (
                100,
                "pl100",
                daily,
                [(1, None, "08:00"), (2, "08:30", "08:31"), (3, "09:00", None)],
            ),
            (
                102,
                "pl100",
                daily,
                [(1, None, "07:30"), (2, "08:00", "08:01"), (3, "08:30", None)],
            ),
            (200, "pl200", daily, [(2, None, "08:36"), (4, "09:20", None)]),
            (201, "pl200", daily, [(2, None, "08:50"), (4, "09:34", None)]),
            (300, "cICE", range(5), [(1, None, "08:10"), (4, "09:30", None)]),
            (400, "pl400", daily, [(3, None, "09:10"), (4, "09:40", None)]),
            (
                500,
                "pl500",
                daily,
                [(1, None, "10:00"), (2, "10:30", "10:31"), (3, "34:00", None)],
            ),
            (
                600,
                "pl600",
                daily,
                [(1, None, "23:50"), (2, "24:20", "24:21"), (4, "25:00", None)],
            ),
            (700, "pl700", [5], [(1, None, "08:05"), (4, "09:00", None)]),
        ]
    )


def make_late_connection_timetable():
    return _build(
        [
            (
                100,
                "pl100",
                range(7),
                [(1, None, "08:00"), (2, "08:30", "08:31"), (3, "09:00", None)],
            ),
            (
                250,
                "pl250",
                range(7),
                [(3, None, "07:50"), (2, "08:35", "08:36"), (4, "09:20", None)],
            ),
        ]
    )
