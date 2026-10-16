# Web Frontend v2 Specification

## Purpose

A second web interface, built from the app's shared UI component library, that is the default
interface at `/`. It works exactly like the current BahnPredict interface: the same layout and
look, the same wording, the same behaviour, and the same requests to and responses from the
backend. The current interface stays available under `/v1` until it is retired.

## Requirements

### Requirement: The second interface is the default, with the same four views
The frontend SHALL serve the second interface at the root: a search view at `/`, a train route
view at `/train/<train id>`, a station board at `/station/<station id>` and a line detail view at
`/line/<line id>`.

#### Scenario: Each view is served at the root
- **WHEN** a user opens `/`, `/train/2259`, `/station/5` or `/line/9`
- **THEN** the second interface's search, train route, station board or line detail view is
  shown

#### Scenario: An unknown id is handled as in the current interface
- **WHEN** a user opens a train, station or line view for an id the backend does not know
- **THEN** exactly the same "not found" or error message is shown as the current interface
  shows, and no data is invented

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
card's window of runs and how it loads, its header name and direction, its starts-here and
ends-here notes, its fixed-height entries and written delays, the board's entries, the station flow on the search view and the board at a chosen moment, the icons,
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

### Requirement: The second interface has its own visual identity
The frontend SHALL give the second interface a visual identity — colour palette, typography,
and component styling — that is independent of the current interface's appearance and
internally consistent across all four views, their loading, empty, error and not-found states,
desktop and phone viewport widths, and light and dark themes.

#### Scenario: Consistent across views and states
- **WHEN** the search, train route, station board and line detail views are shown, including
  their loading, empty, error and not-found states
- **THEN** they share one coherent visual identity — colours, typography, and component styling
  read as one system, not a mix of styles

#### Scenario: Consistent across viewport and theme
- **WHEN** the same view is shown at desktop and phone width, and in light and dark mode
- **THEN** the visual identity holds in every combination; no view falls back to the current
  interface's classes or to shadcn's unstyled generated defaults

#### Scenario: Distinct from the current interface
- **WHEN** the same view and state is shown in both interfaces
- **THEN** the second interface does not render pixel-identical to the current interface

### Requirement: Navigating within the second interface stays within it
Every link and navigation action inside the second interface SHALL lead to a view of the second
interface, never to a view of the current one.

#### Scenario: Opening a train from search results
- **WHEN** a user on `/` presses Prognose and opens one of the listed services
- **THEN** the train view opens at `/train/<id>`, not `/v1/train/<id>`

#### Scenario: Following links from a train page
- **WHEN** a user on a second-interface train page follows its line link or a monitored stop's
  station link
- **THEN** the line view or station board opens at `/line/<id>` or `/station/<id>`

#### Scenario: Going home
- **WHEN** a user presses the app logo or a back action in the second interface
- **THEN** they land on `/`, not `/v1`

### Requirement: Addresses from before the switch keep working
Addresses under `/v2`, where the second interface was served before it became the default, SHALL
redirect to the same view at the root.

#### Scenario: A /v2 address redirects
- **WHEN** a user opens `/v2` or `/v2/<rest>`
- **THEN** they are redirected to `/` or `/<rest>`

### Requirement: Language and theme choices are shared with the current interface
Both interfaces SHALL read and write the same stored language and light/dark choices.

#### Scenario: Language choice is shared
- **WHEN** a user picks English in one interface and then opens the other
- **THEN** the other is also in English

#### Scenario: Theme choice is shared
- **WHEN** a user switches one interface to dark mode and then opens the other
- **THEN** the other is also in dark mode

### Requirement: Every call shows the model's prediction alongside the stored average
Wherever the second interface shows a call's stored average (search results, a train's route, a
station board, a line's runs), it SHALL also show the current model's prediction for that same
call, computed in the browser. Both SHALL be visibly distinguishable as separate values, not
merged into one.

#### Scenario: Both values are shown together
- **WHEN** a call has both a stored average and a model prediction available
- **THEN** the second interface shows both, clearly distinguished, for that call

#### Scenario: Only the average is available
- **WHEN** a call has no model prediction (the model has nothing to run inference with, or
  inference has not finished loading yet)
- **THEN** the second interface shows the average alone, the same way it shows an average alone
  today when no model exists at all

#### Scenario: Only the model prediction is available
- **WHEN** a call has a model prediction but no stored average (its stop is new, or outside the
  averages' history)
- **THEN** the second interface shows the model's prediction alone

#### Scenario: Neither is available
- **WHEN** a call has neither
- **THEN** the second interface shows its existing "no average" state, unchanged

### Requirement: The current interface moves under /v1 and is otherwise unchanged
The frontend SHALL serve the current interface under `/v1`: search at `/v1`, train route at
`/v1/train/<train id>`, station board at `/v1/station/<station id>` and line detail at
`/v1/line/<line id>`. Apart from these addresses, its look, wording and behaviour SHALL NOT
change, and neither SHALL the "page not found" and error pages. Every link and navigation action
inside it SHALL stay under `/v1`.

#### Scenario: Current interface looks as before
- **WHEN** a user opens `/v1`, a train page, a station board or a line page under `/v1`
- **THEN** it looks and behaves exactly as the current interface did before this change

#### Scenario: Current interface links stay under /v1
- **WHEN** a user under `/v1` opens a search result, a train, a station, a line, or presses the
  logo or a back action
- **THEN** the view that opens is under `/v1`

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

### Requirement: The Prognose card steps through runs three at a time, without end
The Prognose card SHALL show 3 runs at the chosen stop at a time: at first the 2 at or before
the chosen time and the 1 after it. Each press of Earlier or Later SHALL move the window by 3
runs. The window SHALL have no end: after a day's last run come the next day's runs, before its
first come the previous day's, however few runs a day the stop has. Both buttons SHALL always be
available once a result is shown, except while a slide is running.

#### Scenario: First result
- **WHEN** a user presses Prognose for 08:00 at a stop served at 07:20, 07:50, 08:20 and 08:50
- **THEN** the card shows the runs at 07:20, 07:50 and 08:20

#### Scenario: Later moves by three runs
- **WHEN** the card shows the runs at positions 1 to 3 and the user presses Later
- **THEN** the card shows the runs at positions 4 to 6

#### Scenario: A stop with few runs goes on into other days
- **WHEN** the stop has 4 runs a day and the user presses Later repeatedly
- **THEN** each press shows the next 3 runs, continuing into the following days, and the button
  never becomes unavailable

#### Scenario: Earlier goes into the day before
- **WHEN** the user presses Earlier past the first run of the chosen day
- **THEN** the card shows the previous day's last runs, each with its own date

### Requirement: Each run shows the day it runs on and is forecast for that day
A run on a day other than the chosen one SHALL be forecast for its own date, not the chosen
date. Every run SHALL show its day (weekday and date) in the same place whether or not it is on
the chosen day. Scheduled times after midnight SHALL read as clock times (00:17, not 24:17).

#### Scenario: A run on the next day
- **WHEN** the user searches for 23:17 and steps Later to a run at 00:17
- **THEN** the run shows the next day's weekday and date, and its forecast is made for that date

#### Scenario: Times past midnight
- **WHEN** a run's scheduled time is stored as 24:17
- **THEN** it is shown as 00:17

### Requirement: The Prognose card loads and forecasts runs ahead of the user
After Prognose, the card SHALL load and forecast the 3 runs it shows first, then load and
forecast the 12 runs before and the 12 runs after them in the background. Whenever the window
comes within 6 runs of the last loaded run in one direction, the card SHALL load and forecast the
next 12 runs in that direction in the background, in both directions without end. The user SHALL
never wait for a request when the next 3 runs are already loaded.

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

### Requirement: Every Prognose entry keeps one fixed height
Every entry in the Prognose card SHALL have exactly the same height in every case: a normal
run, a run that starts or ends at the stop, a run on another day, a run whose forecast is
pending or missing, at desktop and at phone width. An entry SHALL NOT leave large empty space.

#### Scenario: All cases measure the same
- **WHEN** entries of every kind are shown, including after stepping into other days
- **THEN** all entries have the same height

#### Scenario: A note does not make an entry taller
- **WHEN** a run starts or ends at the chosen stop
- **THEN** its entry is no taller than a normal one, with the note in its own place on the entry

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
A run that starts at the chosen stop SHALL say that it starts there and so has no arrival to
forecast, in the place where the delay and expected arrival would be, and SHALL show its
scheduled departure in the place of the arrival. A run that ends at the chosen stop SHALL keep its
forecast and show a note that it ends there. Each note SHALL be readable from text and icon alone,
not only from colour.

#### Scenario: A run starting here
- **WHEN** a shown run has no arrival at the chosen stop because it starts there
- **THEN** the delay and expected columns say there is no arrival and no forecast, the entry
  shows a "starts here" note with its icon, and shows its departure time

#### Scenario: A run ending here
- **WHEN** a shown run has an arrival but no departure at the chosen stop
- **THEN** the entry shows its forecast as usual and an "ends here" note with its icon

#### Scenario: The station board marks the same runs
- **WHEN** a station board lists a call that starts or ends at that station
- **THEN** the call carries the same starts-here or ends-here note as the Prognose card

### Requirement: Each run's times and delay are laid out in labelled columns
The Prognose card SHALL show one column header row, and every entry SHALL put its times in the
same three columns: scheduled, delay and expected, with the delay written as text ("+4 min",
"on time") so colour is never the only way to read it. The whole entry SHALL open the train page.

#### Scenario: A late run
- **WHEN** a run is scheduled to arrive at 08:12 and its forecast delay is 4 minutes
- **THEN** the entry shows "08:12" under scheduled, "+4 min" under delay and "08:16" under
  expected, in the same columns in every entry

#### Scenario: Read without colour
- **WHEN** the card is viewed in greyscale
- **THEN** on-time, slightly late and very late runs can still be told apart from their text

#### Scenario: Opening a run
- **WHEN** the user presses anywhere on an entry
- **THEN** the train page opens at that stop

### Requirement: The Prognose card header shows the direction
Under the searched name, the Prognose card header SHALL show the chosen direction and the stop,
each with its icon. It SHALL NOT repeat the date and time, which are in the form above.

#### Scenario: A line with a direction
- **WHEN** a user pressed Prognose for RE1 towards Saarbrücken Hbf at Trier-Pallien
- **THEN** the header shows "Saarbrücken Hbf" and "Trier-Pallien", and no date or time

### Requirement: Earlier and Later sit beside the entries on wide screens
From tablet width up, the Earlier and Later buttons SHALL sit together on one side of the
entries, vertically centred; on a phone they SHALL sit below the entries.

#### Scenario: Desktop
- **WHEN** a result is shown on a desktop or tablet screen
- **THEN** both buttons are in one column to the right of the entries

#### Scenario: Phone
- **WHEN** a result is shown on a phone
- **THEN** both buttons are in one row below the entries

### Requirement: The station board looks like the Prognose entries
The station board SHALL list its calls as entries of the same layout as the Prognose card:
the same fixed height, two lines in the same columns under one column header row (scheduled,
delay, expected), the same icons, the same starts-here and ends-here notes, the day on the
second line, and the whole entry opening the train page. It SHALL have no Earlier or Later
buttons.

#### Scenario: Same layout
- **WHEN** a station board and a Prognose card are shown side by side
- **THEN** their entries have the same height, columns, icons and notes

#### Scenario: Calls that start or end
- **WHEN** a call starts or ends at the board's station
- **THEN** its entry is no taller than any other and carries the same note as in the Prognose card

#### Scenario: Whole board is scrollable and unchanged in content
- **WHEN** a board lists many calls
- **THEN** all calls of the window are listed in time order, with no stepping controls

### Requirement: Fields and values carry icons
The search form, the Prognose card, the station board and the train page SHALL put an icon next
to each field label and kind of value: state, search, direction (two arrows running opposite
ways), stop or station, date, time, scheduled time, delay, expected arrival, arrival,
departure, and the starts-here and ends-here notes. Each icon SHALL say what it labels (no
download or upload arrows for times). The same kind SHALL use the same icon on every view. Icons
SHALL accompany text and never replace it.

#### Scenario: Search form
- **WHEN** the search view is shown
- **THEN** the state, search, direction, stop, date and time labels each have their own icon

#### Scenario: Same icon everywhere
- **WHEN** an expected arrival is shown on the Prognose card, the station board and the train page
- **THEN** it carries the same expected-arrival icon on all three

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

### Requirement: In dark mode, secondary text stays easy to read
In dark mode, labels and other secondary text SHALL be light enough to read comfortably against
the card background.

#### Scenario: Dark mode labels
- **WHEN** the second interface is shown in dark mode
- **THEN** field labels, column headers and secondary times are clearly legible, not dim grey

### Requirement: The app's icon is a train
The browser tab icon, the touch icon and the icon beside the "BahnPredict" name in the header
SHALL all be the same train symbol, not a generic or a platform logo.

#### Scenario: Tab and header
- **WHEN** any view is open
- **THEN** the tab shows the train icon, and the header shows the same train symbol beside
  "BahnPredict"
