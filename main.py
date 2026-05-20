from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class Station:
    station_id: str
    station_name: str
    trains: List[Train] = field(default_factory=list, repr=False)


@dataclass
class StationStop:
    station: Station
    planned_arrival: Optional[str] = None
    planned_departure: Optional[str] = None


@dataclass
class Train:
    train_id: str
    train_name: str
    path: List[StationStop] = field(default_factory=list)
