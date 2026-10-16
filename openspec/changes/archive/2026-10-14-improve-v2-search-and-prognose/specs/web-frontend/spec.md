## MODIFIED Requirements

### Requirement: A station board shows real calls at a tracked station
The frontend SHALL provide a station board view — reachable independently of a specific train —
listing the calls at a chosen station within a time window around now, or around a date and time
the user chose, each with its scheduled time, platform, the train serving it, and that stop's
historical average delay when available.

#### Scenario: Board lists real upcoming and recent calls
- **WHEN** a user opens the board for a tracked station
- **THEN** the calls listed, their times, and their platforms come from `dbrel`'s recorded data
  for that station within the requested time window

#### Scenario: An untracked station is flagged, not silently empty
- **WHEN** a user opens the board for a station `dbrel` does not track
- **THEN** the frontend explicitly states the station is not tracked, rather than showing an
  empty or misleading board

#### Scenario: Board at a chosen date and time
- **WHEN** a user opens the board for a station with a date of 2026-10-20 and a time of 07:30
- **THEN** the board lists the calls in the window around 07:30 on 2026-10-20, its window line
  shows that date and those times, and the board does not refresh itself every minute

#### Scenario: Board without a chosen date and time stays live
- **WHEN** a user opens the board for a station without a date and time (for example from a
  stop link on a train page)
- **THEN** the board shows the window around now and refreshes itself every minute, as before

#### Scenario: An unusable date or time is rejected
- **WHEN** the board is requested with a date or time that cannot be read
- **THEN** the backend answers with an error saying which value is wrong, and the frontend shows
  its error state instead of a board for some other moment

### Requirement: A search prediction is requested only on explicit request with every input chosen
The search view SHALL request delay predictions only when the user presses the "Prognose"
control, and that control SHALL be unavailable until the user has explicitly chosen all five
inputs: line or train, direction, stop, date, and time. No input SHALL be pre-filled or
assumed on the user's behalf. When a station is chosen instead of a line or train, the control
SHALL need only the station, the date and the time, and SHALL open that station's board.

#### Scenario: Choosing a line or train fills nothing else
- **WHEN** a user chooses a line or train from the search suggestions
- **THEN** direction, stop, date and time all remain unset, and no prediction is requested

#### Scenario: Prognose is unavailable until all five inputs are chosen
- **WHEN** a line or train is chosen and any of direction, stop, date or time is still unset
- **THEN** the Prognose control cannot be activated and no prediction is requested

#### Scenario: Filling every input does not by itself request a prediction
- **WHEN** a user has chosen all five inputs but has not pressed Prognose
- **THEN** no prediction request is sent and no results are shown

#### Scenario: Editing an input withdraws a shown result
- **WHEN** results are shown and the user then changes any of the five inputs
- **THEN** the results are withdrawn until the user presses Prognose again

#### Scenario: A station needs only date and time
- **WHEN** a station is chosen and the date or the time is still unset
- **THEN** the control cannot be activated; once both are set it can, without direction or stop

#### Scenario: Choosing a station fills nothing else
- **WHEN** a user chooses a station from the search suggestions
- **THEN** date and time remain as they were (unset on a fresh search), and no board is opened
  until the control is pressed

### Requirement: Choosing a search suggestion never skips the prediction flow
Choosing a line or a specific train from the search suggestions SHALL keep the user on the
search view and lead into the same input flow. A specific train SHALL resolve to its line's
real directions and stops. Choosing a station SHALL also keep the user on the search view, with
the station's name in the search field and the direction and stop inputs shown as unavailable;
pressing the control after choosing a date and a time SHALL show that station's board at that
date and time underneath the form, on the same page, as a Prognose result is shown.

#### Scenario: Choosing a train does not open its route page
- **WHEN** a user chooses a specific train from the search suggestions
- **THEN** the user stays on the search view, the train's name remains in the search field, and
  the direction and stop choices offered are those of the train's line

#### Scenario: Choosing a train that has no line
- **WHEN** a user chooses a train published without a line (for example an ICE or IC)
- **THEN** the direction and stop choices are those of the route formed by trains of the same
  kind covering the same stretch in either direction, and Prognose lists the services on that
  route around the chosen time, exactly as for a train with a line

#### Scenario: Choosing a station keeps the user on the search view
- **WHEN** a user chooses the station "Aachen Hbf" from the search suggestions
- **THEN** the user stays on the search view, "Aachen Hbf" is in the search field, the direction
  and stop inputs are greyed out and cannot be changed, and the date and time inputs can be set

#### Scenario: Choosing a station opens its board
- **WHEN** a user has chosen a station, the date 2026-10-20 and the time 07:30, and presses the
  control
- **THEN** that station's board for 07:30 on 2026-10-20 appears underneath the form, the address
  stays the same, and the form stays in view

#### Scenario: Editing withdraws the board
- **WHEN** a board is shown and the user changes the search, the date or the time
- **THEN** the board is withdrawn until the control is pressed again

#### Scenario: Old and linked addresses lead to the same place
- **WHEN** a user opens `/station/<id>` (with or without `?date=…&time=…`), or follows a station
  link from a train page
- **THEN** the search page opens with that station in the search field and its board underneath,
  live when no date and time were given

#### Scenario: Switching from a station to a line
- **WHEN** a station is chosen and the user then types and chooses a line instead
- **THEN** the direction and stop inputs become available again for that line, and the control
  goes back to needing all five inputs

## ADDED Requirements

### Requirement: Stops offered for a line are in travel order
The stop choices offered for a line or route and direction SHALL be in the order a train
travels them, merging the stops of every run so that a stop only some runs call at (a branch or
a detour) sits next to the stops it follows and precedes, not at the end of the list.

#### Scenario: A branch stop sits at its junction
- **WHEN** most runs call at A, B, C, D and a few call at A, B, X, D
- **THEN** the stops are offered as A, B, C, X, D

#### Scenario: A stop only a minority call at is not last
- **WHEN** one run calls at a stop the main runs skip
- **THEN** it appears between its neighbours on that run, not after the last stop
