# Stop Delay Averages Specification

## Purpose

Provides a queryable, precomputed average arrival/departure delay for a specific line stopping
at a specific station, in a specific direction, on a specific scheduled service — split by
whether that service runs on a holiday-timetable day — so a user can answer "how late does RE1
usually run through station X, direction A, on the 11:00 service" without recomputing over raw
trip history each time.

## Requirements

### Requirement: Averages are keyed by station, line, direction, trip start time, and scheduled time
Each row in the average table SHALL represent one distinct combination of station, line,
direction, the trip's own origin start time, and scheduled time-of-day at that station — never
a blend of two different combinations.

#### Scenario: Two directions of the same line at the same station stay separate
- **WHEN** RE1 stops at station X while traveling in direction A, and also stops at station X
  while traveling in direction B
- **THEN** the system produces two separate average rows, one per direction, never averaged
  together

#### Scenario: Two distinct daily departures of the same line stay separate
- **WHEN** RE1 in direction A has a service scheduled through station X at 11:00 and a separate
  service scheduled through station X at 15:00
- **THEN** the system produces two separate average rows, one per scheduled time, never
  averaged together

#### Scenario: Two trips reaching the same station at the same scheduled time, from different origins, stay separate
- **WHEN** two different trips of the same line and direction (e.g. coupled portions departing
  different origin stations at different times) happen to call at the same station at the same
  scheduled clock time
- **THEN** the system produces two separate average rows, keyed apart by each trip's own origin
  start time, never averaged together — the scheduled time at the shared station is the same
  for both, but the trip start time is not

#### Scenario: A line id spans multiple unrelated regional services
- **WHEN** a short line name (e.g. "RE7") is shared by several physically unrelated regional
  corridors in different parts of the country
- **THEN** direction is resolved separately within each corridor, so a train in one corridor is
  never classified against another corridor's route as its reference axis

### Requirement: Holiday and non-holiday averages are computed separately
For a given (station, line, direction, trip start time, scheduled time) combination, the system SHALL maintain
distinct averages for holiday-timetable days and non-holiday days, per the holiday-calendar
capability's per-station holiday flag.

#### Scenario: A station's holiday and weekday averages differ
- **WHEN** the same (station, line, direction, trip start time, scheduled time) combination has recorded stops on
  both holiday and non-holiday days
- **THEN** the system reports two separate averages for that combination, one for holiday days
  and one for non-holiday days, and neither figure includes stops from the other day type

### Requirement: Averages are computed only from recorded, non-cancelled observations
Only stops with an observed (not inferred, not absent) delay measurement SHALL contribute to an
average.

#### Scenario: A stop with no recorded observation does not affect the average
- **WHEN** a scheduled stop was never confirmed by any archived or live measurement
- **THEN** that stop does not contribute to any average, and the sample count reflects only
  stops that were actually observed

#### Scenario: A cancelled stop does not affect the average
- **WHEN** a scheduled stop was cancelled
- **THEN** that stop does not contribute to the arrival/departure delay averages

#### Scenario: A stop with an undecidable holiday flag does not affect the average
- **WHEN** a stop's `is_holiday` label is unknown (its station's state is unresolved and the
  date is a state-specific holiday -- see holiday-calendar spec)
- **THEN** that stop contributes to neither the holiday nor the non-holiday average for its
  (station, line, direction, trip start time, scheduled time) combination

### Requirement: Implausibly large delay readings do not contribute to an average
A stop's arrival delay SHALL contribute to `n_arr`/`mean_arr_delay_s` only if its magnitude is
within the plausibility threshold; independently, its departure delay SHALL contribute to
`n_dep`/`mean_dep_delay_s` only if its magnitude is within the plausibility threshold. This is
evaluated per side, not per row.

#### Scenario: A stop with one implausible side still contributes its plausible side
- **WHEN** a stop has an arrival delay whose magnitude exceeds the plausibility threshold but a
  departure delay within it
- **THEN** that stop contributes to `n_dep`/`mean_dep_delay_s` but not to `n_arr`/`mean_arr_delay_s`

#### Scenario: A stop with both sides implausible contributes to neither
- **WHEN** a stop has both an arrival delay and a departure delay whose magnitudes exceed the
  plausibility threshold
- **THEN** that stop contributes to neither side's average, but the row for its (station, line,
  direction, trip start time, scheduled time, holiday flag) combination is still produced if any
  other stop's observation contributes to it

### Requirement: Each average reports its sample size
Every average row SHALL expose how many observations it is based on, alongside the mean delay.

#### Scenario: Querying an average returns its sample count
- **WHEN** a user retrieves the average delay for a (station, line, direction, trip start time, scheduled time,
  holiday flag) combination
- **THEN** the response includes the number of recorded stops the average was computed from

### Requirement: Averages are precomputed, not scored live
The average table SHALL be a materialized, on-demand-rebuilt table, not a value computed fresh
on every read request.

#### Scenario: Repeated queries against an unchanged database return identical results
- **WHEN** the same average is queried twice with no rebuild in between and no new backfill data
  written
- **THEN** both queries return the same value, without recomputing from raw stop data on each
  request
