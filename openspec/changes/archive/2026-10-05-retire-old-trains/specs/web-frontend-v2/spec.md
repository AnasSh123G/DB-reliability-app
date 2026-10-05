# Spec Delta

## ADDED Requirements

### Requirement: A retired train's page says it no longer runs
For a train not seen in the timetable for more than 14 days before the newest data, the train
page SHALL show that it has not run since its last date and may now run under a new number, and
SHALL show no stops. A train without a last-seen date (from the GTFS feeds) SHALL never count as
retired.

#### Scenario: An old link to a retired train
- **WHEN** the page of a train last seen on 2026-08-12 is opened while the newest data is from
  2026-09-28
- **THEN** it says the train has not run since 12 Aug 2026 and may now run under a new number, and
  lists no stops

#### Scenario: A running train is unaffected
- **WHEN** the page of a train last seen within the 14 days is opened
- **THEN** its route is shown as before

### Requirement: Search does not offer retired trains
A search SHALL NOT list a retired train. Stations, lines and running trains SHALL be found as
before, and retired matches SHALL NOT take places in the result list from running ones.

#### Scenario: A number shared by a retired and a running train
- **WHEN** a search for 26405 matches retired RE 26405 and running NX 26405
- **THEN** only NX 26405 is listed
