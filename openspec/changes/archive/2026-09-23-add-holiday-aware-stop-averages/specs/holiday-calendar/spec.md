# Spec Delta

## Purpose

Resolves, for any German date and station, whether that station is running a Sunday/holiday
timetable — correctly accounting for the fact that German public holidays are set per
Bundesland, not nationally, so the same date can be a holiday at one station and an ordinary
weekday at another.

## ADDED Requirements

### Requirement: Station-to-state resolution
Every tracked station SHALL have a resolved German Bundesland (one of the 16 states) derived
from its coordinates.

#### Scenario: A station with valid coordinates resolves to exactly one state
- **WHEN** a station's `(lat, lon)` falls within Germany's borders
- **THEN** the system assigns it exactly one of the 16 Bundesländer

#### Scenario: A station outside Germany is not force-assigned a state
- **WHEN** a station's coordinates fall outside all 16 German state boundaries (e.g. a
  cross-border stop)
- **THEN** the system records no state for it rather than guessing the nearest one, and this
  station is excluded from holiday-flagged averaging until resolved

### Requirement: Per-state public holidays
The system SHALL know, for each of the 16 Bundesländer and for any year in the tracked data
range, which dates are public holidays in that state.

#### Scenario: A state-specific holiday applies only in its own state
- **WHEN** a date is a public holiday in Bavaria but not in Hamburg (e.g. Fronleichnam)
- **THEN** a Bavarian station is flagged as on a holiday timetable for that date, and a Hamburg
  station is not

#### Scenario: A nationwide holiday applies everywhere
- **WHEN** a date is a public holiday in all 16 states (e.g. Tag der Deutschen Einheit)
- **THEN** every tracked station is flagged as on a holiday timetable for that date

### Requirement: Sundays are always holiday-timetable days
Every Sunday SHALL be treated as a holiday-timetable day at every station, regardless of state.

#### Scenario: A Sunday with no coinciding public holiday
- **WHEN** a date falls on a Sunday and is not itself a listed public holiday anywhere
- **THEN** every tracked station is flagged as on a holiday timetable for that date

### Requirement: Holiday flag is per-station, not global
A single calendar date SHALL be capable of being flagged as a holiday timetable day for some
tracked stations and not others, on the same date.

#### Scenario: Querying two stations in different states on the same date
- **WHEN** the same date is queried for a station in a state observing a holiday that day and a
  station in a state not observing it
- **THEN** the two stations return different holiday-flag results for that date

### Requirement: A partial (city/region-only) holiday never flags a whole state
Some public holidays observed in a state are legally restricted to specific municipalities
within it (e.g. Augsburg's Friedensfest, observed only in Augsburg, not all of Bavaria). Such a
holiday SHALL be recorded, but SHALL NOT be treated as applying to every station in that state.

#### Scenario: Augsburg's Friedensfest does not flag all of Bavaria
- **WHEN** a date is Augsburg's Friedensfest, a partial (city-only) holiday within Bayern
- **THEN** the system does not flag a Bavarian station outside Augsburg as on a holiday
  timetable for that date solely because of this holiday

### Requirement: `trip_stop.is_holiday` is the persisted per-stop holiday label
Each `trip_stop` row SHALL carry an explicit `is_holiday` label (1, 0, or unknown), computed
from its own station's state and its trip's origin service_date, so downstream consumers (like
stop-delay-averages) never have to re-derive it per query.

#### Scenario: A stop's station has no resolved state, and the date is state-specific
- **WHEN** a `trip_stop`'s station has an unresolved `state`, and its trip's service_date is a
  public holiday in some states but not all 16
- **THEN** `is_holiday` for that stop is recorded as unknown (NULL) rather than guessed, since
  neither 0 nor 1 can be determined without knowing the station's state

#### Scenario: A stop's station has no resolved state, but the date is decidable anyway
- **WHEN** a `trip_stop`'s station has an unresolved `state`, and its trip's service_date is
  either a Sunday or a public holiday observed in all 16 states
- **THEN** `is_holiday` for that stop is recorded as 1, since neither case depends on knowing
  which state the station is in
