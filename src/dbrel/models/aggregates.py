"""The day-type vocabulary.

Used to be home to the derived rollups (train_station_delay, train_stop_average)
too. Both were dropped: every "how late is this usually" figure the app shows
now comes from ml/live.py, run fresh per request against the trained model,
rather than from a precomputed table read back. What is left here is just the
two-value vocabulary those tables used to key on, which the model still uses as
a feature (see ml/_common.FEATURE_SPEC) and which holidays_de.day_type_of still
classifies a calendar date into.
"""

from __future__ import annotations

# Two buckets, not seven: a public holiday runs a Sunday timetable, so it
# belongs with the Sundays, and everything else pools together. Saturday counts
# as a weekday — it runs a thinner timetable but it is a working day.
# dbrel/holidays_de.py decides which is which.
DAY_TYPE_WEEKDAY = "WEEKDAY"
DAY_TYPE_SUNDAY_HOLIDAY = "SUNDAY_HOLIDAY"
