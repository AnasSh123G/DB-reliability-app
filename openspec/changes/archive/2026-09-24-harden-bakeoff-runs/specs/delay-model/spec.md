# Spec Delta

## Purpose

Trains, evaluates and queries a local model that predicts a scheduled call's arrival and
departure delay from the timetable alone, without reading any stored delay averages at
prediction time.

## ADDED Requirements

### Requirement: Training runs are isolated from the desktop and capped in memory
A bake-off or model search SHALL be started as its own background service. The service SHALL
NOT belong to the process tree or memory accounting group of the editor, the terminal, or the
session that launched it. It SHALL run under a hard memory cap and SHALL NOT use swap. Reaching
the cap SHALL stop only that service. After the service ends, its exit reason (finished, failed
or memory limit) SHALL still be readable.

#### Scenario: Closing the editor does not stop training
- **WHEN** a run was launched and the editor, terminal or launching session is then closed or
  killed
- **THEN** the run keeps going

#### Scenario: A run that exceeds its memory cap is stopped alone
- **WHEN** a run's memory reaches the cap
- **THEN** only that run is stopped, no other process is killed, and its exit reason reports the
  memory limit

#### Scenario: A finished run can still be inspected
- **WHEN** a run has ended, successfully or not
- **THEN** its exit reason and its log can still be read, and starting the next run does not
  overwrite the previous run's log

### Requirement: Reported memory includes swapped memory
Every memory figure a training run logs SHALL include the run's swapped-out memory as well as
its resident memory. The run SHALL also log the peak of its memory group.

#### Scenario: Memory pushed to swap is not reported as freed
- **WHEN** part of a run's memory is moved to swap
- **THEN** the logged total does not fall, and the swapped share is shown

### Requirement: An interrupted run resumes from its last finished step
A run SHALL save each finished step to disk as soon as it is done: every tuning trial, and each
side's final model and predictions. A resumed run SHALL skip every saved step and continue with
the next one, with the tuning budget reduced by the time already spent. A saved step SHALL only
be reused for the same dataset, date split and search settings. Anything else SHALL be refused
with a message, not silently mixed in.

#### Scenario: A kill during a final fit loses only that fit
- **WHEN** a run is killed during the arrival-side final fit of a library whose tuning and
  departure-side fit had finished
- **THEN** resuming starts that library's arrival-side fit directly, without tuning again or
  refitting the departure side

#### Scenario: A kill during tuning keeps the finished trials
- **WHEN** a run is killed during tuning after some trials had finished
- **THEN** resuming continues the same search with those trials counted, and spends only the
  rest of the budget

#### Scenario: Saved work from a different dataset is not reused
- **WHEN** a resume finds saved steps from a different dataset, split or search space
- **THEN** it stops with a message that names the mismatch, and trains nothing

### Requirement: A memory check precedes a full run
Before a full run is launched, a short capped probe SHALL measure each selected library's peak
memory at the tuning sample size and at a fraction of the final-fit size. It SHALL then project
the peak at the full final-fit size. The launcher SHALL refuse to start a full run whose projected
peak exceeds its memory cap, unless explicitly overridden.

#### Scenario: A run that would not fit is not started
- **WHEN** the probe projects a library's final-fit peak above the memory cap
- **THEN** the full run is not launched, and the message names the library, its projected
  peak and the cap
