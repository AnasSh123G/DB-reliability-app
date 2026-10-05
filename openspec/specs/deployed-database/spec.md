# deployed-database Specification

## Purpose
The deployed database is the read-only copy of the collection database that a deployed web
server reads. It holds only what the server needs to answer, in a compact form, and makes the
server answer exactly as it would from the collection database.

## Requirements

### Requirement: The deployed database is built from the collection database without changing it
The system SHALL build the deployed database as a new file from the collection database. The
collection database SHALL only be read: the build SHALL NOT write, add or remove rows in it.

#### Scenario: The collection database is untouched
- **WHEN** the deployed database is built
- **THEN** the collection database's tables hold the same rows as before the build

### Requirement: The deployed database holds only what the server reads
The deployed database SHALL hold the schedule, trains, stations, service and passenger lines,
public holidays, search aliases, the current model's notice-rate totals and the route-variant
run summary. It SHALL NOT hold per-run observations: no dated trip rows, observed stop times,
messages or polls.

#### Scenario: No per-run data is copied
- **WHEN** the deployed database is built
- **THEN** it contains no dated trip rows, no observed stop times and no timetable messages

### Requirement: Identical stop patterns are stored once
Two route variants whose stops agree in order, station, platform, source stop id, pickup and
drop-off type and in every time relative to the variant's earliest scheduled time SHALL share
one stored pattern. Each variant SHALL store only which pattern it follows, its earliest scheduled
time and its rows' ids.

#### Scenario: Two hourly runs share a pattern
- **WHEN** two trains call at the same stations with the same platforms and the same travel
  times, one starting at 08:04 and the other at 09:04
- **THEN** the deployed database stores their stops once, and the two variants differ only in
  their start time and row ids

#### Scenario: A different platform makes a different pattern
- **WHEN** two variants agree in everything except the platform at one stop
- **THEN** they are stored as two patterns

### Requirement: Every schedule row is reproduced exactly
For every stop row of the collection database's schedule, the deployed database SHALL present a
row with the same id, train, route variant, feed, stop order, station, platform, source stop id,
pickup and drop-off type and scheduled arrival and departure times. No row SHALL be added or
lost.

#### Scenario: A stop after midnight keeps its service-day time
- **WHEN** a stop's scheduled arrival is 25:10 (90,600 seconds into the service day)
- **THEN** the deployed database presents the arrival as 90,600 seconds

#### Scenario: A missing time stays missing
- **WHEN** a stop has a departure but no scheduled arrival (an origin)
- **THEN** the deployed database presents no arrival for that stop, not zero

#### Scenario: Row counts agree
- **WHEN** the deployed database is built
- **THEN** it presents exactly as many schedule rows per feed as the collection database holds

#### Scenario: Row ids are the collection database's
- **WHEN** a station board is requested from the deployed database
- **THEN** each call carries the same `station_stop_id` as the collection database's row for it

### Requirement: Route-variant history covers every recorded run
The deployed database SHALL hold, for each train and route variant with at least one recorded
run, the date of its most recent run and its total number of runs, over all recorded runs. This
SHALL let the server choose a train's route variant exactly as it would from the collection
database. A train has at most one recorded run per service date.

#### Scenario: An old variant still counts
- **WHEN** a train's variant last ran 90 days before the newest recorded run
- **THEN** its last run date and run count are present and equal to the collection database's

#### Scenario: The most recent variant is the same
- **WHEN** a train's latest run used a short variant while another variant ran more often
- **THEN** the deployed database names the short variant as the most recent, as the collection
  database does

### Requirement: The build refuses a copy that does not match
The build SHALL rebuild every schedule row from the stored patterns and compare it with the
collection database. It SHALL also confirm that the notice-rate totals match the current model's
fingerprint. On any difference it SHALL fail, name what differs and leave the file at the output
path as it was.

#### Scenario: A rebuilt row differs
- **WHEN** a rebuilt schedule row differs from the collection database's row in any field but
  the id
- **THEN** the build fails, names the train and the stop, and the file at the output path is
  unchanged

#### Scenario: A correct copy is accepted
- **WHEN** every rebuilt row matches and the notice-rate totals match the model
- **THEN** the build finishes and the new file replaces the one at the output path

### Requirement: The build reports its size
On success, the build SHALL report the deployed file's size, the number of schedule rows it
presents, the number of stored patterns and pattern stops, and the number of route-variant
history rows.

#### Scenario: Size and counts are printed
- **WHEN** a build finishes
- **THEN** its output states the file size in MiB, the schedule rows presented, the patterns and
  pattern stops stored, and the route-variant history rows

### Requirement: The server answers from the deployed database as from the collection database
For the same request at the same time, the server SHALL return the same search results, train
routes, station boards, line views, line directions, the earlier and later runs around a chosen
stop and time, and model inputs from the deployed database as from the collection database.

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
