# Spec Delta

## Purpose

Trains, evaluates and queries a local model that predicts a scheduled call's arrival and
departure delay from the timetable alone, without reading any stored delay averages at
prediction time.

## ADDED Requirements

### Requirement: Training rows use only facts knowable before the train runs
Every feature of a training row SHALL be derivable from the timetable, calendar and station data
before the run takes place. No feature SHALL come from the run's own observed times, delays,
cancellations, platform changes or disruption messages, nor from any other run on the same
service date.

The feature set SHALL cover:
- station, train, line, category and operator
- weekday and holiday flag
- scheduled time at the stop, and the trip's start time
- the stop's position along its route
- station coordinates and federal state
- the direction of travel

#### Scenario: A live-only field is not a feature
- **WHEN** the training dataset is built
- **THEN** none of its features is taken from actual times, delays, cancellation flags, changed
  platforms or disruption messages of any run

#### Scenario: Training and prediction build features the same way
- **WHEN** a feature row is built for a historical stop during training, and for the same
  scheduled call during prediction
- **THEN** both rows contain identical feature values

### Requirement: Only trustworthy observations become training targets
A stop SHALL become a training target only if its observation is recorded (not a forecast or
missing), it is not cancelled, and the delay for that side (arrival or departure) is within the
same plausibility limit the stored averages use. Arrival and departure SHALL be judged
independently.

#### Scenario: Cancelled stops are excluded
- **WHEN** a stop is cancelled
- **THEN** it contributes to neither the arrival nor the departure target

#### Scenario: An implausible delay on one side does not discard the other
- **WHEN** a stop's arrival delay exceeds the plausibility limit but its departure delay does not
- **THEN** the stop contributes a departure target and no arrival target

### Requirement: Evaluation splits by whole service dates in time order
Training, validation and test sets SHALL be disjoint ranges of whole service dates, in that
chronological order. No service date SHALL appear in more than one set. The test dates SHALL be
used only for the final scoring, never for tuning or model selection.

#### Scenario: No run is split across sets
- **WHEN** the dataset is split
- **THEN** every stop of a given run lies in the same set as every other stop of that run

#### Scenario: Tuning never sees test days
- **WHEN** hyperparameters are searched or a candidate is chosen
- **THEN** only training and validation dates are used

### Requirement: Candidate models are compared against baselines on the same test days
The system SHALL train and tune LightGBM, CatBoost and XGBoost candidates. Each SHALL predict the
median, 25th and 75th percentile of arrival and departure delay. On the same test days, each SHALL
be scored against:
- an averages baseline built from the training days only, with the stored averages' key
- a global-median baseline

The report SHALL give, per candidate, per baseline and per side:
- mean absolute error of the median, in minutes
- pinball loss for each percentile
- the share of test targets inside the 25th–75th percentile band
- the share of test targets the candidate could score at all

#### Scenario: The averages baseline does not see the test days
- **WHEN** the averages baseline is scored
- **THEN** its averages are computed from training-day observations only, and the stored
  averages tables are not read

#### Scenario: A combination the averages never saw is still reported
- **WHEN** a test target's key has no training-day average
- **THEN** the report counts it as not covered for the averages baseline and does not invent a
  value, while the model candidates still score it

#### Scenario: The report names a winner by validation, not test
- **WHEN** the bake-off finishes
- **THEN** the report marks which candidate was chosen, using validation performance only, and
  shows every candidate's and baseline's test scores next to it

### Requirement: Predicted percentiles are ordered
For every prediction the 25th percentile SHALL NOT exceed the median, and the median SHALL NOT
exceed the 75th percentile.

#### Scenario: Crossed quantile outputs are corrected
- **WHEN** a candidate's raw outputs for a call are out of order
- **THEN** the returned 25th, 50th and 75th percentiles are in non-decreasing order

### Requirement: A trained model is saved as a versioned artifact
Each training run SHALL write a new artifact directory that is never overwritten. The directory
SHALL contain:
- the chosen model for each side
- every candidate's scores
- the feature list and the category vocabularies
- the training, validation and test date ranges
- the library versions used

A pointer SHALL identify which artifact the prediction command uses.

#### Scenario: Retraining keeps the previous artifact
- **WHEN** training runs a second time
- **THEN** a new artifact directory is created, the previous one is untouched, and the pointer
  moves to the new one only after it was written completely

### Requirement: Delays can be predicted offline from the command line
The CLI SHALL predict, for a chosen train or line, station, date and time, the arrival and
departure delay as a median with a 25th–75th percentile band. It SHALL use only the saved model
and the timetable, calendar and station data. It SHALL NOT read stored delay averages or observed
trip data.

#### Scenario: Prediction works without any averages
- **WHEN** the stored averages tables are empty and a user requests a prediction for a scheduled
  call
- **THEN** a median and band are still printed for each side the call has

#### Scenario: A call the model never saw in training still gets a prediction
- **WHEN** a user requests a prediction for a train and station whose combination has no
  training observations
- **THEN** a prediction is printed, based on the call's other features

#### Scenario: A terminating call has no departure prediction
- **WHEN** the requested call has no scheduled departure
- **THEN** no departure prediction is printed, and none is derived from the arrival

#### Scenario: No trained model exists
- **WHEN** a user requests a prediction before any model has been trained
- **THEN** the command fails with a message saying which training command to run, and prints
  no figures
