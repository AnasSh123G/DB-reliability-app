"""All ORM models. Importing this module registers every mapper."""

from dbrel.models.aggregates import (
    DAY_TYPE_SUNDAY_HOLIDAY,
    DAY_TYPE_WEEKDAY,
    TrainStationDelay,
    TrainStopAverage,
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
from dbrel.models.meta import FeedVersion, PublicHoliday, Route
from dbrel.models.timetables import (
    TtMessage,
    TtPlanFetch,
    TtPoll,
    TtUnmatched,
)

__all__ = [
    "DAY_TYPE_SUNDAY_HOLIDAY",
    "DAY_TYPE_WEEKDAY",
    "QUALITY_CANCELLED",
    "QUALITY_NO_DATA",
    "QUALITY_PREDICTION_ONLY",
    "QUALITY_RECORDED",
    "TRUSTED_QUALITIES",
    "FeedVersion",
    "PublicHoliday",
    "Route",
    "SearchAlias",
    "ServiceLine",
    "Station",
    "StationStop",
    "Train",
    "TrainStationDelay",
    "TrainStopAverage",
    "Trip",
    "TripStop",
    "TtMessage",
    "TtPlanFetch",
    "TtPoll",
    "TtUnmatched",
]
