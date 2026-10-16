## MODIFIED Requirements

### Requirement: The deployed database holds only what the server reads
The deployed database SHALL hold the schedule, trains, stations, service and passenger lines,
public holidays, search aliases, the current model's notice-rate totals, the route-variant
run summary and the weekday running profile. It SHALL NOT hold per-run observations: no dated
trip rows, observed stop times, messages or polls. It SHALL NOT hold the schedule rows, run-summary
rows, running-profile rows or search aliases of retired trains, nor values for train columns the
server never reads.

#### Scenario: No per-run data is copied
- **WHEN** the deployed database is built
- **THEN** it contains no dated trip rows, no observed stop times and no timetable messages

#### Scenario: Retired trains keep only their train row
- **WHEN** the deployed database is built and a train is retired
- **THEN** its train row is present, and it has no schedule rows, run-summary rows,
  running-profile rows or search aliases

### Requirement: The build reports its size
On success, the build SHALL report the deployed file's size, the number of schedule rows it
presents, the number of stored patterns and pattern stops, the number of route-variant
history rows and the number of running-profile rows with the profile's first and last date.

#### Scenario: Size and counts are printed
- **WHEN** a build finishes
- **THEN** its output states the file size in MiB, the schedule rows presented, the patterns and
  pattern stops stored, the route-variant history rows, and the running-profile rows with the
  window's first and last date

### Requirement: The server answers from the deployed database as from the collection database
For the same request at the same time, the server SHALL return the same search results, train
routes, station boards, line views, line directions, the earlier and later runs around a chosen
stop and time, model inputs, journey plans and one route's other times from the deployed
database as from the collection database.

#### Scenario: A station board matches
- **WHEN** the board of one station is requested for the same window from a server on each
  database
- **THEN** the two responses are identical

#### Scenario: The runs around a chosen time match
- **WHEN** the runs before and after a chosen time at a chosen stop are requested for a line, and
  for a train without a line, from a server on each database
- **THEN** both responses list the same runs in the same order with the same times and model
  inputs

#### Scenario: A train route matches
- **WHEN** the route of a train with several route variants is requested from a server on each
  database, opened from a stop only one variant calls at
- **THEN** both responses show the same variant with the same stops, times and model inputs

#### Scenario: A journey plan matches
- **WHEN** the same plan (stations, date, time) is requested from a server on each database with
  the same model
- **THEN** both responses list the same connections with the same trains, times and forecasts,
  apart from the search-statistics fields

## ADDED Requirements

### Requirement: The deployed database stores a weekday running profile
For each running train and route variant, the deployed database SHALL store how many runs it
had on each weekday (Monday to Sunday) within the profile window. The window is the 28 service
dates ending at the newest recorded service date. Dates that are a statewide (not city-only)
public holiday in any state SHALL be left out of the counts. Weekdays with no runs SHALL have no row.

#### Scenario: A weekday train
- **WHEN** a train ran on each of the 4 Mondays to Fridays of the window and on no weekend day
- **THEN** its variant has rows for weekdays 0 to 4 with 4 runs each (fewer where a weekday was a
  holiday), and none for 5 and 6

#### Scenario: Runs before the window are not counted
- **WHEN** a variant's last run was 40 days before the newest recorded service date
- **THEN** it has no running-profile rows

### Requirement: The running profile is the same from either database
The running profile the server reads SHALL be identical whether it comes from the deployed
database's stored rows or is computed from the collection database's recorded runs. The build
SHALL compare the two and fail, naming the train, on any difference.

#### Scenario: A mismatching profile fails the build
- **WHEN** a stored profile row differs from the one computed from recorded runs
- **THEN** the build fails, names the train and variant, and leaves the file at the output path
  unchanged
