# Archive Backfill Specification

## Purpose

Populates the reliability database with historical train stops recovered from a public
archive of the same Deutsche Bahn timetable responses dbrel collects live, so that history
predating this installation's own collection becomes queryable — while guaranteeing that a
stop the archive never observed is recorded as unobserved, never as punctual.

## Requirements

### Requirement: Backfill from an archived response mirror

The system SHALL provide a command that populates `trip` and `trip_stop` rows from
archived Deutsche Bahn Timetables `/plan` and `/fchg` responses obtained from a public
mirror, without requiring Timetables API credentials.

Backfilled rows SHALL be written through the same ingest path as live collection, carrying
the same `rule_version`, the same set of quality grades, and the same delay-cause records,
so that a query cannot distinguish a backfilled stop from a live-collected one by anything
other than its recorded provenance and poll timing.

#### Scenario: Backfill without credentials

- **WHEN** the operator runs the backfill command on an installation that has no
  Timetables API key configured
- **THEN** the backfill completes and writes stops to the database
- **AND** no request is made to the Timetables API

#### Scenario: Backfilled rows are gradeable like live rows

- **WHEN** a backfilled stop is written to the fact table
- **THEN** it carries a quality grade drawn from the same set live collection uses
- **AND** it carries the same `rule_version` a live-collected stop of that shape would
- **AND** any delay-cause messages present in the archived response are recorded against it

### Requirement: An unobserved stop is never recorded as punctual

When the archive contains a stop's planned time but no published actual time for it, the
system SHALL record that stop as having no actual time and no delay, under the quality
grade that denotes absent observation.

The system MUST NOT substitute the planned time for a missing actual time, and MUST NOT
derive a zero delay from such a substitution.

#### Scenario: Stop present in the schedule but never reported

- **WHEN** the archive holds a `/plan` entry for a stop
- **AND** no archived `/fchg` response for that stop carries an actual time
- **THEN** the stop is recorded with no actual arrival or departure time
- **AND** its delay fields are empty rather than zero
- **AND** its quality grade denotes absent observation, not punctual running

#### Scenario: Stop reported as genuinely punctual

- **WHEN** an archived `/fchg` response publishes an actual time for a stop that equals
  its planned time
- **THEN** the stop is recorded with that actual time
- **AND** its delay is zero
- **AND** its quality grade denotes a recorded observation

#### Scenario: The two cases stay distinguishable after backfill

- **WHEN** a genuinely punctual stop and a never-reported stop are both backfilled
- **THEN** the two rows differ in quality grade
- **AND** a query filtering on the recorded-observation grade returns the punctual stop and
  excludes the never-reported one

### Requirement: Observations are judged against their recording instant

The system SHALL treat an archived response as evidence only of what was known at the
instant that response was recorded. Whether an event had already occurred SHALL be
determined by comparing the event's time against the archived response's own recording
timestamp, not against the time the backfill runs.

An actual time published before its event occurred is a forecast, and the system MUST NOT
record it as an observed actual.

#### Scenario: Forecast published ahead of the event

- **WHEN** an archived response recorded at 00:57 carries an actual time for a stop
  scheduled at 04:00
- **AND** no later archived response reports that stop
- **THEN** the stop is not credited with an observed actual time from that response
- **AND** its quality grade does not denote a recorded observation

#### Scenario: Later response supersedes an earlier forecast

- **WHEN** an archived response recorded at 00:57 carries a forecast for a stop scheduled
  at 04:00
- **AND** a later archived response recorded at 15:20 carries an actual time for the same stop
- **THEN** the value from the later response is recorded as the observed actual
- **AND** its quality grade denotes a recorded observation

#### Scenario: Backfill run time does not affect grading

- **WHEN** the same archive window is backfilled twice, weeks apart
- **THEN** both runs produce the same quality grades and the same actual times for the
  stops in that window

### Requirement: Bounded, resumable acquisition

The backfill SHALL acquire only an explicitly bounded time window of archived data,
defaulting to the most recent 30 days, and the window SHALL be configurable by the
operator.

Acquisition SHALL be resumable: an interrupted or repeated run SHALL NOT re-download
archive partitions it already holds intact, and SHALL report what it downloaded versus
reused.

#### Scenario: Default window

- **WHEN** the operator runs the backfill without specifying a window
- **THEN** only archive partitions covering the last 30 days are acquired

#### Scenario: Resuming after interruption

- **WHEN** a backfill download is interrupted and the command is run again for the same window
- **THEN** partitions already held intact are reused rather than re-downloaded
- **AND** the run reports the counts of reused and newly downloaded partitions

#### Scenario: Window boundaries include overnight runs

- **WHEN** the requested window begins on a given day
- **THEN** the acquisition also covers enough of the adjacent day for a run crossing
  midnight into the window to be assembled completely

### Requirement: Gaps in the archive are recorded as gaps

Where the archive holds no response covering a stop's observation window, the system SHALL
record that absence as a measured gap, carrying the width of the gap, under the quality
grade that denotes an uncovered window.

The system MUST NOT represent an archive gap as an on-time stop or omit it silently.

#### Scenario: Hours missing from the archive

- **WHEN** the archive contains no responses for a range of hours within the requested window
- **THEN** stops whose observation window falls in that range are graded as having an
  uncovered window
- **AND** the recorded gap width reflects the distance to the nearest covering response
- **AND** the run reports the missing hours to the operator

### Requirement: Backfill does not disturb live collection

Backfilling SHALL be additive with respect to live collection. Re-running a backfill over a
window already covered SHALL NOT duplicate stops, and SHALL NOT replace a stop observed
live with a less-well-observed archived one.

Live collection behaviour SHALL be unchanged by the presence of this capability.

#### Scenario: Backfill overlapping live-collected data

- **WHEN** a window that live collection already covered is backfilled
- **THEN** no duplicate stop rows are created
- **AND** a stop already carrying a recorded observation is not downgraded to a
  weaker grade

#### Scenario: Repeated backfill is idempotent

- **WHEN** the same archive window is backfilled twice with no new archive data in between
- **THEN** the second run leaves the fact table's contents unchanged

#### Scenario: Live collection unaffected

- **WHEN** live collection runs on an installation where backfill has been used
- **THEN** it grades and writes stops exactly as it would on an installation where it
  has not

### Requirement: Station scope follows the existing tracked set

The backfill SHALL write stops only for stations the installation already tracks, and
SHALL NOT introduce station rows from the archive on its own.

Where the archive covers stations the installation does not track, or the installation
tracks stations the archive does not cover, the run SHALL report both counts.

#### Scenario: Archive covers an untracked station

- **WHEN** the archive holds responses for a station the installation does not track
- **THEN** no stops are written for that station
- **AND** the run reports how many archived stations were skipped as untracked

#### Scenario: Tracked station absent from the archive

- **WHEN** the installation tracks a station for which the archive holds no responses
- **THEN** the run reports that station as uncovered by the archive
- **AND** the backfill completes for the remaining stations
