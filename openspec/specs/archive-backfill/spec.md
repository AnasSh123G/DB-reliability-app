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

### Requirement: A stale run reference does not abort a replayed day

When a `/fchg` stop refers to a run whose trip moved to a different route variant earlier in
the same sweep, the system SHALL file that stop's schedule evidence under the trip's current
route variant. The system SHALL NOT raise an error that rolls back the day.

#### Scenario: Trip re-pointed mid-sweep

- **WHEN** a sweep first adds a run that moves an existing trip to a new route variant
- **AND** a later stop in the same sweep refers to that trip through a reference loaded before
  the move
- **THEN** the later stop's evidence is stored under the trip's current route variant
- **AND** the day's replay completes and is recorded as done

#### Scenario: Real archive days replay without the path error

- **WHEN** the reference sample days are replayed into a fresh database
- **THEN** no day fails with a path-mismatch error

### Requirement: Optimized replay produces the same database as the reference implementation

Replaying the same archive days into identical starting databases SHALL produce the same rows
with the reference implementation and with the optimized one. Rows are compared on natural
keys, not surrogate ids. The only allowed differences are `train.n_runs_seen` and
`train.n_stops_tracked`, and only when per-partition plan import is enabled.

#### Scenario: Fresh database, sample window

- **WHEN** a sample window is replayed into two fresh databases seeded with the same reference
  tables, one by each implementation
- **THEN** the comparison reports zero differing, missing or extra rows in every compared
  table, apart from the allowed counter columns

#### Scenario: Large existing database

- **WHEN** the same days are replayed into two reflink copies of the live database, one by
  each implementation
- **THEN** the comparison, restricted to those service dates, reports zero differences apart
  from the allowed counter columns

#### Scenario: Comparison catches a real difference

- **WHEN** a single fact value is altered in one of two otherwise identical databases
- **THEN** the comparison exits non-zero and names the table, the natural key and the column

### Requirement: Replay throughput is measured and reported

The backfill SHALL report, on request, the wall time each replayed day spends in plan import,
change sweeps and finalize. A benchmark SHALL report the per-day wall time of the reference
and optimized implementations on the same days, both on a fresh database and on a copy of the
live database.

#### Scenario: Timings on request

- **WHEN** the operator runs the backfill with timings enabled
- **THEN** each day prints the seconds spent in plan import, change sweeps and finalize

#### Scenario: Speedup accepted

- **WHEN** the benchmark compares the two implementations on copies of the live database
- **THEN** the optimized implementation's end-to-end time per day is at most one third of
  the reference implementation's
- **AND** the report states the projected time for a full year at the measured rate

#### Scenario: Measured baseline day

- **WHEN** archive day 2026-09-29 is replayed into a copy of the live database taken after
  2026-10-07
- **THEN** the optimized implementation finishes in at most one third of the reference's
  time on the same copy. That reference time was 26 min 43 s on 2026-10-17.

### Requirement: Replay cost does not grow with accumulated history

The time to replay one archive day SHALL depend on that day's archived responses. It SHALL
NOT depend on how many earlier days, trips, polls or unfinalized trips the database already
holds.

#### Scenario: Unfinalizable history is not revisited

- **WHEN** the database holds trips from earlier service dates that finalization can never
  close, for example trips with no planned stop evidence
- **THEN** replaying a later day does not load or re-examine those trips

#### Scenario: Overdue trips survive archive gaps

- **WHEN** an earlier trip remains unfinished and intervening archive days are missing or
  contain plans without change sweeps
- **THEN** a later eligible sweep still finalizes that trip with reference-equivalent facts,
  regardless of the number of intervening days

### Requirement: Bulk-load tuning is confined to the backfill

Database settings that trade memory for load speed SHALL apply only while a backfill runs.
They SHALL be bounded so that a backfill's peak memory stays under the documented cap. Live
collection and the web server SHALL keep their current connection settings.

#### Scenario: Live collection settings unchanged

- **WHEN** a collection cycle or the web server opens a database connection after this change
- **THEN** it uses the same cache, memory-map and synchronous settings as before the change

#### Scenario: Backfill under the memory cap

- **WHEN** a backfill of one day runs under the documented memory cap
- **THEN** it completes without being killed for memory

### Requirement: Backfill and live collection never write the same database at once

A backfill SHALL hold an exclusive writer lock on its target database for as long as it
runs. When the target is the default live database, it SHALL also exclude a running live
collection. A backfill into a scratch database SHALL keep its resume record outside the shared
archive directory when the operator asks for that.

#### Scenario: Backfill while collection holds the live database

- **WHEN** a live collection is running against the default database
- **AND** the operator starts a backfill against the same database
- **THEN** the backfill exits with an "already running" message and writes nothing

#### Scenario: Scratch backfill keeps its own resume record

- **WHEN** the operator runs a backfill into a scratch database with a separate resume file
- **THEN** the days it completes are recorded only in that file
- **AND** the archive directory's resume record is left unchanged

### Requirement: Develop optimizations on a bounded sample before scaling

The implementation workflow SHALL verify each speed change on a small representative
sample and focused regression tests before running it on full-database copies. Broad
multi-day reference runs SHALL NOT block starting implementation.

#### Scenario: Small sample iteration

- **WHEN** a speed optimization is implemented
- **THEN** a bounded real archive sample is compared strictly against the frozen reference
- **AND** larger real-data validation follows once the sample passes
