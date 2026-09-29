"""A small file-backed database for the delay-model tests.

File-backed, not in-memory, because the dataset builder reads the database
through its own read-only sqlite3 connection, exactly as it does in production.

Two lines' worth of structure:
- RE 7 (line 7): trains 70 (Köln -> Hamm) and 71 (Hamm -> Köln), three tracked
  stops each; train 70 is observed on every date in DATES.
- ICE 10/11 (no line): 10 runs Köln -> Berlin, 11 runs Berlin -> Köln, 12 runs
  within the stretch (Hannover -> Berlin).

Delays follow a simple, deterministic rule (they are fixture values in a
throwaway test database, never written anywhere real).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from dbrel.db import Base, make_engine
from dbrel.identity import SOURCE_TT
from dbrel.ingest.runs import tt_feed_version
from dbrel.models import ServiceLine, Station, StationStop, Train, Trip, TripStop

BERLIN = ZoneInfo("Europe/Berlin")
START = date(2026, 9, 1)
DATES = [(START + timedelta(days=i)).isoformat() for i in range(10)]

STATIONS = {
    1: ("Köln Hbf", 50.94, 6.96, "DE-NW"),
    2: ("Dortmund Hbf", 51.52, 7.46, "DE-NW"),
    3: ("Hamm(Westf)Hbf", 51.68, 7.81, "DE-NW"),
    4: ("Hannover Hbf", 52.38, 9.74, "DE-NI"),
    5: ("Berlin Hbf", 52.53, 13.37, None),  # unresolved state, on purpose
}


def utc_of(service_date: str, local_sec: int) -> int:
    base = datetime.fromisoformat(service_date).replace(tzinfo=BERLIN)
    return int((base + timedelta(seconds=local_sec)).timestamp())


def _stops(train_id: int, stops: list[tuple[int, int | None, int | None]], fv: int) -> list[StationStop]:
    return [
        StationStop(feed_version_id=fv, train_id=train_id, stop_sequence=10 * (i + 1),
                    station_id=st, planned_arrival_sec=arr, planned_departure_sec=dep,
                    path_sha1="main")
        for i, (st, arr, dep) in enumerate(stops)
    ]


PATTERNS = {
    70: [(1, None, 8 * 3600), (2, 8 * 3600 + 3000, 8 * 3600 + 3120), (3, 9 * 3600 + 600, None)],
    71: [(3, None, 17 * 3600), (2, 17 * 3600 + 1200, 17 * 3600 + 1320), (1, 18 * 3600, None)],
    # Starts before midnight, calls after it: the start-time wrap case.
    10: [(1, None, 23 * 3600), (4, 26 * 3600, 26 * 3600 + 120), (5, 28 * 3600, None)],
    11: [(5, None, 6 * 3600), (4, 8 * 3600, 8 * 3600 + 120), (1, 11 * 3600, None)],
    12: [(4, None, 12 * 3600), (5, 14 * 3600, None)],
}


def add_run(path, train_id: int, service_date: str, delay_s: int = 120) -> None:
    """One more recorded run of `train_id` on `service_date` (new trip id, same pattern)."""
    engine = make_engine(path)
    with Session(engine) as s:
        trip_id = (s.query(Trip.id).order_by(Trip.id.desc()).first()[0]) + 1
        s.add(Trip(id=trip_id, train_id=train_id, service_date=service_date, source=SOURCE_TT,
                   source_run_key=f"{train_id}-extra-{trip_id}", status="FINAL", path_sha1="main"))
        ss_ids = {r.station_id: r.id for r in s.query(StationStop).filter_by(train_id=train_id)}
        for seq, (st, arr, dep) in enumerate(PATTERNS[train_id]):
            s.add(TripStop(
                trip_id=trip_id, stop_sequence=10 * (seq + 1), station_id=st, station_stop_id=ss_ids[st],
                supposed_arrival_utc=None if arr is None else utc_of(service_date, arr),
                supposed_departure_utc=None if dep is None else utc_of(service_date, dep),
                arr_delay_s=None if arr is None else delay_s, dep_delay_s=None if dep is None else delay_s,
                quality="RECORDED", rule_version=1, is_holiday=0,
            ))
        s.commit()
    engine.dispose()


def build(path, dates: list[str] = DATES) -> None:
    engine = make_engine(path)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        fv = tt_feed_version(s)
        for sid, (name, lat, lon, state) in STATIONS.items():
            s.add(Station(id=sid, natural_key=f"{name}@{lat},{lon}", name=name, lat=lat, lon=lon,
                          state=state, tracked=1))
        s.add(ServiceLine(id=7, identity_key="DB/RE7", category="RE", line="7",
                          display_name="RE7", search_name="re7"))
        names = {sid: v[0] for sid, v in STATIONS.items()}
        for tid, cat, number, line in ((70, "RE", 10070, 7), (71, "RE", 10071, 7),
                                       (10, "ICE", 10, None), (11, "ICE", 11, None),
                                       (12, "ICE", 12, None)):
            pattern = PATTERNS[tid]
            s.add(Train(
                id=tid, source=SOURCE_TT, identity_key=f"tts:{cat}/{number}", category=cat,
                segment="FV" if cat == "ICE" else "RV", operator="DB", train_number=number,
                display_name=f"{cat} {number}", search_name=f"{cat.lower()}{number}", line_id=line,
                origin_name=names[pattern[0][0]], dest_name=names[pattern[-1][0]],
                route_path="|".join(names[st] for st, _, _ in pattern),
                path_sha1="main", first_departure_sec=pattern[0][2], n_stops=len(pattern),
            ))
        s.flush()
        for tid, pattern in PATTERNS.items():
            s.add_all(_stops(tid, pattern, fv))
        s.flush()
        ss_ids = {(r.train_id, r.station_id): r.id for r in s.query(StationStop)}

        trip_id = 0
        for day_i, service_date in enumerate(dates):
            for tid, pattern in PATTERNS.items():
                trip_id += 1
                start = pattern[0][2]
                stamp = datetime.fromisoformat(service_date).strftime("%y%m%d") + \
                    f"{(start // 3600) % 24:02d}{(start % 3600) // 60:02d}"
                s.add(Trip(id=trip_id, train_id=tid, service_date=service_date, source=SOURCE_TT,
                           source_run_key=f"{tid}-{stamp}", status="FINAL", path_sha1="main"))
                for seq, (st, arr, dep) in enumerate(pattern):
                    delay = 60 * ((tid + day_i + seq) % 5)
                    s.add(TripStop(
                        trip_id=trip_id, stop_sequence=10 * (seq + 1), station_id=st,
                        station_stop_id=ss_ids[(tid, st)],
                        supposed_arrival_utc=None if arr is None else utc_of(service_date, arr),
                        supposed_departure_utc=None if dep is None else utc_of(service_date, dep),
                        arr_delay_s=None if arr is None else delay,
                        dep_delay_s=None if dep is None else delay,
                        quality="RECORDED", rule_version=1,
                        is_holiday=1 if datetime.fromisoformat(service_date).weekday() == 6 else 0,
                    ))
        s.commit()
    engine.dispose()
