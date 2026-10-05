# Spec Delta

## MODIFIED Requirements

### Requirement: The deployed database holds only what the server reads
The deployed database SHALL hold the schedule, trains, stations, service and passenger lines,
public holidays, search aliases, the current model's notice-rate totals and the route-variant
run summary. It SHALL NOT hold per-run observations: no dated trip rows, observed stop times,
messages or polls. It SHALL NOT hold the schedule rows, run-summary rows or search aliases of
retired trains, nor values for train columns the server never reads.

#### Scenario: No per-run data is copied
- **WHEN** the deployed database is built
- **THEN** it contains no dated trip rows, no observed stop times and no timetable messages

#### Scenario: Retired trains keep only their train row
- **WHEN** the deployed database is built and a train is retired
- **THEN** its train row is present, and it has no schedule rows, run-summary rows or search aliases

### Requirement: Every schedule row is reproduced exactly
For every stop row of a running train in the collection database's schedule, the deployed
database SHALL present a row with the same id, train, route variant, feed, stop order, station,
platform, source stop id, pickup and drop-off type and scheduled arrival and departure times. No
such row SHALL be added or lost.

#### Scenario: A stop after midnight keeps its service-day time
- **WHEN** a stop's scheduled arrival is 25:10 (90,600 seconds into the service day)
- **THEN** the deployed database presents the arrival as 90,600 seconds

#### Scenario: A missing time stays missing
- **WHEN** a stop has a departure but no scheduled arrival (an origin)
- **THEN** the deployed database presents no arrival for that stop, not zero

#### Scenario: Row counts agree
- **WHEN** the deployed database is built
- **THEN** it presents exactly as many schedule rows per feed as the collection database holds
  for running trains

#### Scenario: Row ids are the collection database's
- **WHEN** a station board is requested from the deployed database
- **THEN** each call carries the same `station_stop_id` as the collection database's row for it

### Requirement: Route-variant history covers every recorded run
The deployed database SHALL hold, for each running train and route variant with at least one
recorded run, the date of its most recent run and its total number of runs, over all recorded
runs. This SHALL let the server choose a train's route variant exactly as it would from the
collection database. A train has at most one recorded run per service date.

#### Scenario: An old variant still counts
- **WHEN** a running train's variant last ran 90 days before the newest recorded run
- **THEN** its last run date and run count are present and equal to the collection database's

#### Scenario: The most recent variant is the same
- **WHEN** a train's latest run used a short variant while another variant ran more often
- **THEN** the deployed database names the short variant as the most recent, as the collection
  database does
