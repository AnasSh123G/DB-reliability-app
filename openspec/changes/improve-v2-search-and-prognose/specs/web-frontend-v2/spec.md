## MODIFIED Requirements

### Requirement: The second interface behaves and communicates exactly like the current one
For every view, state, viewport width, theme and language, the second interface SHALL match the
current interface in everything except visual appearance:
- the same controls with the same behaviour, including the browser's native select, date and
  time inputs wherever the current interface uses them
- the same wording in German and English
- the same behaviour for every action: what the user can activate, what it does, and what it
  leads to
- the same API requests for the same actions (the same endpoints and parameters, sent at the
  same moments), with each response rendered the same way

It SHALL add no behaviour the current interface lacks and drop none it has, except where another
requirement of this specification describes the second interface's own behaviour: the Prognose
card's window of runs and how it loads, its header name, its starts-here and ends-here notes, its
written delays, the station flow on the search view and the board at a chosen moment, the icons,
the shared width, the logo row and the highlighted stop on the train page. Its visual
appearance is governed by "The second interface has its own visual identity", not by this
requirement.

#### Scenario: Requests are identical
- **WHEN** a user performs the same sequence of actions in both interfaces (typing a query,
  choosing a line or train suggestion, choosing direction, stop, date and time, pressing
  Prognose, opening a train, a station or a line, widening an empty board)
- **THEN** both interfaces send the same API requests, with the same endpoints and parameters,
  in the same order, apart from the requests that load the Prognose card's runs and the board
  at a chosen moment, which follow their own requirements

#### Scenario: Behaviour is identical
- **WHEN** a user presses a key, clicks, or navigates back in the second interface
- **THEN** the result is the same as the same input in the current interface. This covers
  Prognose staying disabled until all five inputs are chosen for a line or train, the suggestion
  list closing on an outside click, the search being kept on return within the visit and reset
  by a full reload, and "no average" shown where there is no average.

#### Scenario: The listed differences are the only ones
- **WHEN** the two interfaces are compared action by action
- **THEN** every difference in behaviour is one of those named in this requirement, and the
  current interface under `/v1` is unchanged

## ADDED Requirements

### Requirement: The Prognose card steps through runs three at a time
The Prognose card SHALL show 3 runs at the chosen stop at a time: at first the 2 at or before
the chosen time and the 1 after it. Each press of Earlier or Later SHALL move the window by 3
runs, or by fewer when fewer are left that way, so the window never wraps around the day into the
runs of the first result. A button SHALL be unavailable once no further run is left that way.

#### Scenario: First result
- **WHEN** a user presses Prognose for 08:00 at a stop served at 07:20, 07:50, 08:20 and 08:50
- **THEN** the card shows the runs at 07:20, 07:50 and 08:20

#### Scenario: Later moves by three runs
- **WHEN** the card shows the runs at positions 1 to 3 and the user presses Later
- **THEN** the card shows the runs at positions 4 to 6

#### Scenario: A stop with few runs
- **WHEN** the stop has 4 distinct runs in the chosen direction and the card shows runs 1 to 3
- **THEN** Later moves the window by one run to show runs 2 to 4, and after that Later is
  unavailable

#### Scenario: Fewer than three runs
- **WHEN** the stop has 2 distinct runs in the chosen direction
- **THEN** the card shows both, once each, and Earlier and Later are unavailable

### Requirement: The Prognose card loads and forecasts runs ahead of the user
After Prognose, the card SHALL load and forecast the 3 runs it shows first, then load and
forecast the 12 runs before and the 12 runs after them in the background. Whenever the window
comes within 6 runs of the last loaded run in one direction, the card SHALL load and forecast the
next 12 runs in that direction in the background. The user SHALL never wait for a request when
the next 3 runs are already loaded.

#### Scenario: Shown runs come first
- **WHEN** a user presses Prognose
- **THEN** the 3 runs shown and their forecasts appear without waiting for the 24 runs around
  them, which are loaded and forecast afterwards

#### Scenario: A step within loaded runs is immediate
- **WHEN** the 12 runs after the window are loaded and the user presses Later
- **THEN** the next 3 runs appear without a new request

#### Scenario: Two steps trigger the next chunk
- **WHEN** after the first result the user presses Later twice, so the window is 6 runs from the
  last loaded run that way
- **THEN** the next 12 later runs are requested and forecast in the background, and the window
  keeps responding while they load

#### Scenario: The day wraps around
- **WHEN** the runs loaded reach the end of the day's schedule
- **THEN** further runs continue from the start of the day, and no run appears twice in the
  loaded set

### Requirement: The Prognose card names the searched line or train once
The Prognose card SHALL show, once in its header, the name the user chose in search (for example
"RE1" or "ICE 612"), together with the chosen stop. The runs of a line SHALL NOT repeat a train
name or show the train's operational number ("RB 10478").

#### Scenario: A line
- **WHEN** a user searched for the line "RE1" and pressed Prognose
- **THEN** the header shows "RE1" and the stop, and each run shows only its times, forecast and
  notes

#### Scenario: A train picked by name
- **WHEN** a user picked the regional train "RB 10478", which runs as line RB22
- **THEN** the header shows the name the user picked, not a different internal number

#### Scenario: A route of trains without a line
- **WHEN** a user picked "ICE 612", whose route is served by differently numbered trains
- **THEN** the header shows "ICE 612" and each run that is not ICE 612 shows its own number, so a
  run is never labelled with a number it does not run as

### Requirement: The Prognose card explains runs that start or end at the chosen stop
A run that starts at the chosen stop SHALL show a clearly visible note that it starts there and
so has no arrival to forecast, and SHALL show its scheduled departure. A run that ends at the
chosen stop SHALL show an equally visible note that it ends there. Each note SHALL be readable
from text and icon alone, not only from colour.

#### Scenario: A run starting here
- **WHEN** a shown run has no arrival at the chosen stop because it starts there
- **THEN** in place of the forecast, the row says that the train starts at this stop, so there
  is no arrival to forecast, and shows its departure time

#### Scenario: A run ending here
- **WHEN** a shown run has an arrival but no departure at the chosen stop
- **THEN** the row shows its arrival forecast as usual and a visible note that the train ends at
  this stop

#### Scenario: The station board marks the same runs
- **WHEN** a station board lists a call that starts or ends at that station
- **THEN** the call carries the same starts-here or ends-here note as the Prognose card

### Requirement: Each run's times and delay are labelled and written out
Every run in the Prognose card SHALL show labelled parts in a fixed layout: scheduled arrival,
forecast delay and expected arrival. The forecast delay SHALL be written as text ("+4 min",
"on time"), so colour is never the only way to read it.

#### Scenario: A late run
- **WHEN** a run is scheduled to arrive at 08:12 and its forecast delay is 4 minutes
- **THEN** the row shows "08:12" labelled as scheduled arrival, "+4 min" labelled as delay, and
  "08:16" labelled as expected arrival, in the same places in every row

#### Scenario: Read without colour
- **WHEN** the card is viewed in greyscale
- **THEN** on-time, slightly late and very late runs can still be told apart from their text

### Requirement: Fields and values carry icons
The search form, the Prognose card, the station board and the train page SHALL put an icon next
to each field label and kind of value: state, search, direction, stop or station, date, time,
arrival, departure, delay, and the starts-here and ends-here notes. The same kind SHALL use the
same icon on every view. Icons SHALL accompany text and never replace it.

#### Scenario: Search form
- **WHEN** the search view is shown
- **THEN** the state, search, direction, stop, date and time labels each have their own icon

#### Scenario: Same icon everywhere
- **WHEN** an arrival time is shown on the Prognose card, the station board and the train page
- **THEN** it carries the same arrival icon on all three

### Requirement: All views share one content width
The search form, its hint, the Prognose card, the station board, the train page and the line
page SHALL all use the same content width at each viewport width.

#### Scenario: Search view
- **WHEN** a Prognose result is shown under the search form on a desktop screen
- **THEN** the form, the hint and the result card have the same left and right edges

#### Scenario: Across views
- **WHEN** the user moves from the search view to a train page and to a station board
- **THEN** the main card keeps the same width

### Requirement: The logo shares a row with the theme and language toggles
The header SHALL put the logo and the BahnPredict name on the left and the theme and language
toggles on the right, vertically centred in one row, at every viewport width.

#### Scenario: Desktop and phone
- **WHEN** any view is shown at desktop or phone width
- **THEN** the logo, the name and the toggles are in one row, centred on one line

### Requirement: The train page highlights the stop the user came from
When a train page is opened from a stop (from the Prognose card or a station board), its route
SHALL visibly highlight that stop, on desktop and on phone, in a way readable without colour.

#### Scenario: Opened from Prognose
- **WHEN** a user pressed Prognose at "Düren" and opens one of the runs
- **THEN** the train page highlights Düren in the route with a marker and a label, and no other
  stop is highlighted

#### Scenario: Opened directly
- **WHEN** a train page is opened without a stop (for example from a line page or an address)
- **THEN** no stop is highlighted
