# Web Frontend Specification

## Purpose

Defines the `dbrel` web frontend that riders and operators use to look up trains, stations, and
lines and see their delay figures — replacing the Django-rendered pages with a client app whose
every displayed value comes from real `dbrel` data, never fabricated or hardcoded content.

## Requirements

### Requirement: The shipped frontend contains no mock or synthetic rail data
No hardcoded train, station, or delay dataset SHALL be reachable from the built frontend.
Every train, stop, platform, and delay figure a user sees SHALL originate from a request to the
`dbrel` backend, not from a value baked into the frontend's source.

#### Scenario: Searching returns real trains
- **WHEN** a user searches for a line or train number
- **THEN** the results reflect trains actually present in the `dbrel` database, not a fixed
  small set of hardcoded sample trains

#### Scenario: No fabricated reverse-direction timetable
- **WHEN** a user views a train's opposite-direction service
- **THEN** the app shows an actual opposite-direction train recorded by `dbrel`, never a
  timetable synthesized by reversing and re-timing the original train's own stops

#### Scenario: No fabricated recurring-service spacing
- **WHEN** a user looks up the services around a given time at a stop
- **THEN** the app shows the actual scheduled services nearest that time, never a fixed-interval
  sequence invented independently of the schedule

### Requirement: Per-stop delay figures are drawn from precomputed historical averages
Wherever the frontend shows an "expected" or "predicted" arrival or departure time for a stop,
that figure SHALL be the precomputed mean delay for that (station, line, direction, trip start
time, scheduled time, holiday flag) combination, not a value invented by the frontend and not a
live per-trip forecast.

#### Scenario: An average exists for the stop
- **WHEN** a stop being displayed has a corresponding row with at least one sample in the
  historical average data
- **THEN** the frontend shows the expected arrival/departure as the scheduled time offset by
  that row's mean delay, labeled as a historical/typical figure rather than a live prediction

#### Scenario: No average exists for the stop
- **WHEN** a stop being displayed has no corresponding average row, or the row has zero samples
  for that side (arrival or departure)
- **THEN** the frontend explicitly indicates no average is available for that side, and does not
  invent, zero-fill, or otherwise fabricate a delay figure

### Requirement: The route view distinguishes monitored from unmonitored stops
A train's full route SHALL show every scheduled stop, and SHALL visibly mark which of those
stops `dbrel` actually monitors (has recorded schedule/delay data for) versus which are named
from the train's path but not tracked.

#### Scenario: An unmonitored stop shows no fabricated times
- **WHEN** a stop on a train's route is not one of the stations `dbrel` tracks
- **THEN** the frontend shows that stop as not monitored and shows no scheduled or expected
  time for it, rather than omitting the stop or inventing a time

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

### Requirement: A line detail view shows real runs in both directions
The frontend SHALL provide a line detail view — reachable independently of a specific train —
listing every scheduled run of a chosen line in each direction, each with its scheduled
departure time, destination, and historical average delay when available.

#### Scenario: Both directions are listed separately
- **WHEN** a user opens the detail view for a line
- **THEN** the runs are grouped by direction, using `dbrel`'s own direction resolution for that
  line, not an assumption derived from the frontend's own logic

#### Scenario: A replacement-bus line is flagged
- **WHEN** a line is recorded as a rail-replacement bus service
- **THEN** the line detail view visibly indicates this, matching the warning already surfaced by
  the equivalent server-rendered view today

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

### Requirement: Search suggestions are distinguishable and name only real lines
Each line and train in the search suggestions SHALL show its end stations beneath its name.
Different lines that share a name SHALL be listed separately. A line whose name is only a
generic product-category code (for example "RE", "RB", "S") SHALL NOT be offered. A named
service without a number (for example "FEX", "HSB", "SEV") SHALL still be offered.

#### Scenario: Same-name lines are told apart by their end stations
- **WHEN** several unrelated lines share a name such as "RE22"
- **THEN** each is listed separately with its own end stations beneath the name

#### Scenario: A category-only line name is not offered
- **WHEN** a user searches for "RE"
- **THEN** no line whose name is just "RE" is offered, while individual numbered trains
  remain searchable

#### Scenario: A named service without a number is offered
- **WHEN** a user searches for "FEX"
- **THEN** the FEX line is offered

### Requirement: Direction choices are labelled by destination station
Each direction choice SHALL be labelled with the name of the station that direction runs
towards, with no surrounding wording.

#### Scenario: Direction label is the station name alone
- **WHEN** a user opens the direction choices for a line running towards Trier Hbf
- **THEN** that choice reads "Trier Hbf"

### Requirement: The suggestion list closes on an outside click
The search suggestion list SHALL close when the user clicks anywhere outside the search field
and its list.

#### Scenario: Clicking elsewhere dismisses the list
- **WHEN** the suggestion list is open and the user clicks outside it
- **THEN** the list closes without choosing anything

### Requirement: The search is kept when returning to it
Within the same visit, returning to the search view from another view SHALL restore the
user's previous inputs and any shown results. A full page reload SHALL start a fresh search.

#### Scenario: Returning from a train page restores the search
- **WHEN** a user opens a train from the search results and then goes back
- **THEN** the search view shows the same five inputs and the same results as before

#### Scenario: Reloading starts fresh
- **WHEN** the user reloads the page
- **THEN** the search view starts with no inputs chosen

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
