"""All ORM models. Importing this module registers every mapper."""

from dbrel.models.aggregates import (
    BASIS_REAL,
    BASIS_SYNTH,
    TrainStationDelay,
)
from dbrel.models.core import (
    QUALITY_CANCELLED,
    QUALITY_NO_DATA,
    QUALITY_PREDICTION_ONLY,
    QUALITY_RECORDED,
    TRUSTED_QUALITIES,
    SearchAlias,
    ServiceLine,
    Station,
    StationStop,
    Train,
    Trip,
    TripStop,
)
from dbrel.models.meta import FeedVersion, Route
from dbrel.models.timetables import (
    TtMessage,
    TtPlanFetch,
    TtPoll,
    TtUnmatched,
)

__all__ = [
    "BASIS_REAL",
    "BASIS_SYNTH",
    "QUALITY_CANCELLED",
    "QUALITY_NO_DATA",
    "QUALITY_PREDICTION_ONLY",
    "QUALITY_RECORDED",
    "TRUSTED_QUALITIES",
    "FeedVersion",
    "Route",
    "SearchAlias",
    "ServiceLine",
    "Station",
    "StationStop",
    "Train",
    "TrainStationDelay",
    "Trip",
    "TripStop",
    "TtMessage",
    "TtPlanFetch",
    "TtPoll",
    "TtUnmatched",
]
