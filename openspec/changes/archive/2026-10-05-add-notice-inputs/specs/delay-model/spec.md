# Spec Delta

## MODIFIED Requirements

### Requirement: Training rows use only facts knowable before the train runs
Every feature of a training row SHALL be derivable from the timetable, calendar and station data
before the run takes place, or be a historical notice rate that meets the notice-rate requirement
below. No feature SHALL come from the run's own observed times, delays, cancellations, platform
changes or messages, nor from any other run on the same service date.

The feature set SHALL cover:
- station, train, line, category and operator
- weekday and holiday flag
- the service date's time of year, from its day and month only
- scheduled time at the stop, and the trip's start time
- the stop's position along its route
- station coordinates and federal state
- the direction of travel
- the historical notice rates of the station, line and train

The time-of-year feature SHALL NOT encode the year, and SHALL be cyclic: the last day of December
and the first day of January SHALL be as close as any two consecutive days.

#### Scenario: A live-only field is not a feature
- **WHEN** the training dataset is built
- **THEN** none of its features is taken from actual times, delays, cancellation flags, changed
  platforms or messages of the run itself or of any run on the same service date

#### Scenario: Training and prediction build features the same way
- **WHEN** a feature row is built for a historical stop during training, and for the same
  scheduled call during prediction
- **THEN** both rows contain identical feature values, given the same stored notice rates

#### Scenario: The same calendar day in different years
- **WHEN** feature rows are built for the same scheduled call on 2025-11-20 and on 2026-11-20
- **THEN** their time-of-year values are identical

#### Scenario: The year boundary is not a jump
- **WHEN** time-of-year values are computed for 31 December and 1 January
- **THEN** their distance equals the distance between any other two consecutive days, within
  rounding

## ADDED Requirements

### Requirement: Notice rates describe how often a place or train has notices
The system SHALL offer six numeric inputs per arrival: for each of the station, the line and the
train, the share of past arrivals whose trip had a construction notice and the share whose trip
had a disruption notice. A construction notice is a stored message whose category starts with
"Bauarbeiten"; a disruption notice one whose category starts with "Störung" or "Großstörung".
Only arrivals eligible as training targets SHALL be counted.

A rate SHALL be smoothed toward the rate one level up (train toward line, line toward station,
station toward all stations) with a recorded weight, so that a key with few arrivals stays close
to its parent. A key with no arrivals at all SHALL give a missing value, never zero, and the
model's input layout SHALL include the missing indicator for it. The line level SHALL be missing
for a train without a line.

#### Scenario: A busy station's rate is its share of arrivals
- **WHEN** a station has 10,000 counted arrivals, 1,200 of them on trips with a disruption notice
- **THEN** its disruption rate is close to 0.12, differing from it only by the smoothing toward
  the all-station rate

#### Scenario: A rarely seen train stays near its line
- **WHEN** a train has 3 counted arrivals, all on trips with a construction notice, and its line's
  construction rate is 0.10
- **THEN** the train's construction rate is far below 1.0 and closer to 0.10 than to 1.0

#### Scenario: A train with no history is missing, not zero
- **WHEN** a train has no counted arrivals in the dates the rates come from
- **THEN** its train-level rates are missing and the input's missing indicator is 1

### Requirement: Notice rates never come from the row's own period
A training row's notice rates SHALL be computed from the training dates outside the calendar
month of the row's service date. Rates for validation rows, test rows and prediction SHALL be
computed from the training dates only. No date after the end of the training range SHALL
contribute to any rate used by a model trained on that range.

#### Scenario: A training row does not count its own month
- **WHEN** the rates of a training row on 2026-03-14 are computed
- **THEN** no arrival from March 2026 contributes to them

#### Scenario: Validation and test days stay out
- **WHEN** the rates for a validation or test row, or for a live prediction, are computed
- **THEN** only arrivals on training dates contribute

#### Scenario: A notice caused by a delay cannot reach its own arrival
- **WHEN** a trip has a disruption notice because its train was already late
- **THEN** that notice is not part of any rate the same trip's arrivals are trained or scored with

### Requirement: Prediction uses stored notice rates only
Predictions SHALL read notice rates from stored data and SHALL NOT call any external service or
read live notices. The artifact SHALL record which training dates its rates come from and the
smoothing weight, and a model that uses the rates SHALL refuse to predict when the stored data
no longer matches that record. The rates SHALL NOT be part of the browser download. Artifacts
without the rates in their input list SHALL predict as before.

#### Scenario: No external call
- **WHEN** a forecast is requested for a trip a week from now
- **THEN** its notice-rate inputs are read from stored data and no network request is made

#### Scenario: Changed rate data is refused
- **WHEN** the stored notice counts for the artifact's recorded dates have changed since training
- **THEN** prediction with that artifact fails with a message naming the mismatch, rather than
  using different rates than the model was trained with

#### Scenario: An older artifact is unaffected
- **WHEN** an artifact trained before this change is used
- **THEN** its input is built from its own recorded input list and the notice rates are not read

### Requirement: The search reports what the notice rates add
The compact track SHALL include an ablation trial that repeats the best network without the
notice-rate inputs, in addition to the existing time-of-year ablation. The ranking and the final
report SHALL show both ablations' validation average error next to the winner's.

#### Scenario: The ablation is ranked on the same rows
- **WHEN** the compact track finishes
- **THEN** the ranking lists the no-notice-rates trial with its validation average error on the
  same arrivals as every other trial
