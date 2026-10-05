# live-accuracy-study Specification

## Purpose
Checks how well TabM and the stored averages actually predict delay, by comparing both against
real arrivals and departures fetched live from the DB Timetables API — outcomes the training
data, the collection schedule and the earlier offline bake-off never touched.

## Requirements

### Requirement: Stations are sampled for a live check
The system SHALL sample, at random and without replacement, a configurable number of stations
(3,000 by default) from the full station list for a live accuracy check.

#### Scenario: Default sample size
- **WHEN** a check is run with no sample size given
- **THEN** 3,000 distinct stations are sampled

#### Scenario: No station is sampled twice
- **WHEN** a check samples its stations
- **THEN** every sampled station appears at most once

### Requirement: Only already-realized stops are scored
For each sampled station, the system SHALL fetch that station's current data directly from the
DB Timetables API and consider only stops with a real, published outcome on a side: a changed
time is present for that side, it is far enough in the past to be a fact rather than a live
estimate, and the side is not cancelled. Arrival and departure SHALL be judged independently: a
stop with no such outcome yet on a side contributes nothing for that side.

#### Scenario: A fully future stop is excluded
- **WHEN** a sampled station has a stop whose arrival and departure are both still scheduled or
  forecast only, with no published time old enough to be a fact
- **THEN** that stop contributes neither an arrival nor a departure comparison

#### Scenario: A near-term estimate is not mistaken for a realized outcome
- **WHEN** a stop's arrival carries a changed time that IRIS published as a live estimate for a
  train that has not yet arrived
- **THEN** that side is not scored until enough time has passed for the time to be treated as a
  fact

#### Scenario: A cancelled side is not scored
- **WHEN** a stop's arrival or departure is cancelled
- **THEN** that side contributes no comparison, even if it carries a changed time

#### Scenario: One side realized, the other not
- **WHEN** a stop's arrival is realized but its departure is not yet
- **THEN** the check scores that stop's arrival and skips its departure

### Requirement: Predictions match what the deployed system actually produces
For each realized stop, the system SHALL compute TabM's prediction from the currently deployed
model artifact, and the stored average exactly as the live app's station board would show it for
that same stop, using the same average lookup the station board uses. Neither figure SHALL be
substituted with training-only or research-only logic that differs from what is actually
deployed.

#### Scenario: TabM prediction matches the deployed artifact
- **WHEN** the check computes TabM's prediction for a realized stop
- **THEN** it is the same prediction `dbrel ml predict-delay` would give for that same
  scheduled call, from the same currently-deployed model

#### Scenario: Average matches what the app would show
- **WHEN** the check looks up the stored average for a realized stop
- **THEN** it is the same figure (or absence of one) the station board API would return for
  that stop at that time

### Requirement: Both predictors are scored against the same real outcomes
The system SHALL score both TabM's and the stored average's prediction against the real recorded
delay for every realized side, and report, for each predictor: mean absolute error, pinball loss,
and the share of outcomes inside the predicted band, broken down at least by day and by segment
and holiday flag.

#### Scenario: A report compares both predictors
- **WHEN** a check completes
- **THEN** its report gives MAE, pinball loss and band coverage for both TabM and the stored
  average, for arrival and departure separately

#### Scenario: A side with no stored average still scores TabM
- **WHEN** a realized stop has no matching stored average
- **THEN** the report counts that side as not covered for the average, without inventing a
  value, while TabM's prediction for that side is still scored

### Requirement: A live check is read-only and self-contained
Running a live accuracy check SHALL NOT modify `trip_stop`, the averages tables, or any trained
model artifact. Its only output SHALL be a report.

#### Scenario: The database is unchanged after a run
- **WHEN** a live accuracy check finishes, successfully or not
- **THEN** no row in `trip_stop`, `stop_delay_average`, or `train_stop_delay_average` has
  changed, and no model artifact has been added, removed or modified

### Requirement: A single station's failure does not fail the whole run
If a sampled station's live fetch fails or returns nothing usable, the system SHALL skip that
station, count it as skipped in the report, and continue with the remaining sample.

#### Scenario: An unreachable station is skipped, not fatal
- **WHEN** a sampled station's live fetch errors or returns no realized stops
- **THEN** the check continues with the rest of the sample and the report names how many
  stations were skipped and why
