# Spec Delta

## MODIFIED Requirements

### Requirement: Training rows use only facts knowable before the train runs
Every feature of a training row SHALL be known before the forecast cutoff of the call it
describes, and SHALL come from one of these sources: the timetable, the calendar, station data,
notices published before the cutoff, earlier runs finalized before the cutoff, or weather
forecasts issued before the cutoff. No feature SHALL come from the run's own observed times,
delays, cancellations or platform changes, from a notice published after the cutoff, nor from any
other run on the same service date.

The feature set SHALL cover:
- station, train, line, category and operator
- weekday and holiday flag
- the service date's time of year, from its day and month only
- scheduled time at the stop, and the trip's start time
- the stop's position along its route
- station coordinates and federal state
- the direction of travel
- announced construction and disruption notices (see "Announced notices are inputs")
- recent delays of the same train and line (see "Recent delays are inputs")
- the weather forecast (see "Weather forecasts are inputs")

The time-of-year feature SHALL NOT encode the year, and SHALL be cyclic: the last day of December
and the first day of January SHALL be as close as any two consecutive days.

A model artifact SHALL record the list of features it was trained on, and prediction SHALL build
exactly that list. An artifact trained before this change keeps its own shorter list.

#### Scenario: A live-only field is not a feature
- **WHEN** the training dataset is built
- **THEN** none of its features is taken from the actual times, delays, cancellation flags or
  changed platforms of the run being described, or of any other run on the same service date

#### Scenario: A notice published after the cutoff is not used
- **WHEN** a construction notice for a trip is first published two hours before a call's
  scheduled arrival, and the call's cutoff is five hours before it
- **THEN** that call's training row counts no notice from it

#### Scenario: Training and prediction build features the same way
- **WHEN** a feature row is built for a historical stop during training, and for the same
  scheduled call during prediction with the same information available
- **THEN** both rows contain identical feature values

#### Scenario: The same calendar day in different years
- **WHEN** feature rows are built for the same scheduled call on 2025-11-20 and on 2026-11-20
- **THEN** their time-of-year values are identical

#### Scenario: The year boundary is not a jump
- **WHEN** time-of-year values are computed for 31 December and 1 January
- **THEN** their distance equals the distance between any other two consecutive days, within
  rounding

#### Scenario: An older artifact keeps its inputs
- **WHEN** the served two-sided TabM `20260924T183944Z` predicts a call after this change
- **THEN** its input is built from its own recorded feature list and is identical to the input
  it received before this change

## ADDED Requirements

### Requirement: Announced notices are inputs
A call's features SHALL include:
- the number of construction notices on its trip
- the number of disruption notices on its trip
- whether any construction notice applies to its own stop

Only notices published at or before the cutoff and not withdrawn by then SHALL count. Notices of
other kinds (general information) SHALL NOT count. When the project holds no plan data for the
call's trip at the cutoff, as for dates beyond the published plan, these features SHALL be
missing, not zero.

#### Scenario: A construction notice announced days ahead
- **WHEN** a construction notice for a trip was published three days before its service date
- **THEN** every call of that trip counts one construction notice, at any cutoff after its
  publication

#### Scenario: A withdrawn notice
- **WHEN** a notice was published and then withdrawn before the cutoff
- **THEN** it does not count

#### Scenario: No plan yet
- **WHEN** a call is predicted for a date whose plan the project has not imported
- **THEN** its notice features are missing, and the model still predicts

### Requirement: Recent delays are inputs
A call's features SHALL include:
- this train's average arrival delay at this station over the 7 service dates before the call's
  service date, and the number of runs behind it
- the same average and count for all trains of its line at this station

Only runs finalized at or before the cutoff SHALL count, and only arrivals that qualify as
training targets. When no run qualifies, the average SHALL be missing and the count zero.

#### Scenario: A train late all week
- **WHEN** a train arrived 8, 9, 7, 10 and 8 minutes late at a station on the five previous days
  and all five runs were finalized before the cutoff
- **THEN** its recent average there is 8.4 minutes over 5 runs

#### Scenario: A run finalized after the cutoff
- **WHEN** yesterday's run was finalized after today's call's cutoff
- **THEN** yesterday's run does not count for that call

#### Scenario: A train with no recent runs
- **WHEN** a train did not run at a station in the previous 7 days
- **THEN** its train-level recent average is missing with a count of zero, and the line-level
  values are still filled if the line ran there

### Requirement: Weather forecasts are inputs
A call's features SHALL include the forecast temperature, precipitation, snowfall and maximum
wind gust at its station's location for its scheduled hour, taken from the latest forecast
issued at or before the cutoff. Training SHALL use archived forecasts issued at least one day
before the scheduled hour, never observed weather. When no forecast issued before the cutoff
covers the scheduled hour, the weather features SHALL be missing.

#### Scenario: Training never sees the weather that happened
- **WHEN** a training row is built for an arrival on a stormy afternoon
- **THEN** its weather features come from a forecast issued at least a day before that
  afternoon, not from observations

#### Scenario: A date beyond the forecast
- **WHEN** a call is predicted for a date further ahead than the stored forecasts reach
- **THEN** its weather features are missing, and the model still predicts

### Requirement: Inputs unknown at prediction time are marked missing
A notice, recent-delay or weather feature that is unavailable at prediction time SHALL be
encoded as missing, distinguishable from a real zero. Training data SHALL contain rows with
each such feature missing, in a share recorded in the model's manifest, so the model has
learnt that case. A prediction SHALL be produced even when all of them are missing.

#### Scenario: Everything unknown
- **WHEN** a call is predicted 20 days ahead, beyond the plan and the weather forecast, for a
  train with no runs in the last 7 days
- **THEN** a prediction is still produced, from the remaining features

#### Scenario: Missing is not zero
- **WHEN** one call has zero announced notices and another call's notices are unknown
- **THEN** their encoded inputs differ

### Requirement: Reports show the share of large misses
Every accuracy report SHALL give, next to the average absolute error, the share of eligible
arrivals missed by more than 3 minutes and by more than 5 minutes. It SHALL break these down by
whether the call had an announced construction notice.

#### Scenario: Large misses in the headline
- **WHEN** a validation or test report is written
- **THEN** its headline lists the average absolute error and the shares missed by more than 3
  and more than 5 minutes, for every model compared

### Requirement: The value of the known-ahead inputs is measured
The full-year search SHALL include a trial of its best network without the notice, recent-delay
and weather features, scored on the same validation arrivals. The report SHALL show the
difference in average absolute error and in the share missed by more than 5 minutes.

#### Scenario: The ablation is reported
- **WHEN** the compact track's search finishes
- **THEN** its ranking includes the without-known-ahead-inputs trial, and the report states both
  differences
