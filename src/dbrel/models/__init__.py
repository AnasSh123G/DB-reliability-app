"""All ORM models. Importing this module registers every mapper."""

from dbrel.models.core import (
    QUALITY_INFERRED_DOWNSTREAM,
    QUALITY_LAST_SEEN,
    QUALITY_MISSING,
    QUALITY_NO_DATA,
    QUALITY_OBSERVED,
    QUALITY_PREDICTION_ONLY,
    QUALITY_SKIPPED,
    TRUSTED_QUALITIES,
    Station,
    StationStop,
    Train,
    Trip,
    TripStop,
)
from dbrel.models.meta import FeedVersion, Route, TripServiceDate
from dbrel.models.realtime import RtObservation, RtPoll, RtUnmatched

__all__ = [
    "QUALITY_INFERRED_DOWNSTREAM",
    "QUALITY_LAST_SEEN",
    "QUALITY_MISSING",
    "QUALITY_NO_DATA",
    "QUALITY_OBSERVED",
    "QUALITY_PREDICTION_ONLY",
    "QUALITY_SKIPPED",
    "TRUSTED_QUALITIES",
    "FeedVersion",
    "Route",
    "RtObservation",
    "RtPoll",
    "RtUnmatched",
    "Station",
    "StationStop",
    "Train",
    "Trip",
    "TripServiceDate",
    "TripStop",
]
