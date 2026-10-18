# Spec Delta

## ADDED Requirements

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

#### Scenario: Consecutive days cost about the same

- **WHEN** seven consecutive archive days are replayed in one run into a copy of the live
  database
- **THEN** no day after the first takes more than 1.25 times as long as the first

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

### Requirement: A rebuild can be compared with the current database

The system SHALL provide a report comparing a fresh rebuild of chosen service dates with the
same dates in an existing database. The report SHALL give, per date:

- trip and stop counts;
- counts per quality grade;
- the share of stops present in both;
- how well recorded delays agree.

#### Scenario: Report over sample dates

- **WHEN** the operator compares a scratch rebuild with the current database for chosen dates
- **THEN** the report lists, per date, the counts on each side, the matched share, and the
  share of matched recorded stops whose delays agree exactly
- **AND** it lists example stops for each kind of disagreement

### Requirement: Develop optimizations on a bounded sample before scaling

The implementation workflow SHALL verify each speed change on a small representative
sample and focused regression tests before running it on full-database copies. Broad
multi-day reference runs SHALL NOT block starting implementation.

#### Scenario: Small sample iteration

- **WHEN** a speed optimization is implemented
- **THEN** a bounded real archive sample is compared strictly against the frozen reference
- **AND** larger real-data validation follows once the sample passes
