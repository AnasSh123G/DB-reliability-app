## MODIFIED Requirements

### Requirement: The station board looks like the Prognose entries
The station board SHALL list its calls as entries of the same layout as the Prognose card: the
same fixed height, the same columns under one column header row (scheduled, delay, expected),
the same icons, the same starts-here and ends-here notes, and the day on the second line. On the
lookup view an entry SHALL choose its run; elsewhere it SHALL open the train page. It SHALL have
no Earlier or Later buttons.

#### Scenario: Same layout
- **WHEN** a station board and a Prognose card are shown side by side
- **THEN** their entries have the same height, columns, icons and notes

#### Scenario: Calls that start or end
- **WHEN** a call starts or ends at the board's station
- **THEN** its entry is no taller than any other and carries the same note as in the Prognose card

#### Scenario: Whole board is scrollable and unchanged in content
- **WHEN** a board lists many calls
- **THEN** all calls of the window are listed in time order, with no stepping controls

### Requirement: The train page highlights the stop the user came from
When a route is shown for a stop (the lookup's chosen run, or a train page opened from a stop),
the stop SHALL be marked by a bar at its side, a tinted background and its name in teal, with no
label, so it reads without colour. No other stop SHALL be marked.

#### Scenario: Opened from Prognose
- **WHEN** a user pressed Prognose at "Düren" and chooses one of the runs
- **THEN** its route marks Düren with the bar and background, and no other stop is marked

#### Scenario: Opened directly
- **WHEN** a train page is opened without a stop (for example from a line page or an address)
- **THEN** no stop is marked

### Requirement: The Prognose card loads and forecasts runs ahead of the user
After Prognose, the card SHALL load and forecast the 6 runs it shows first, then load and
forecast the 12 runs before and the 12 runs after them in the background. Whenever the window
comes within 6 runs of the last loaded run in one direction, the card SHALL load and forecast the
next 12 runs in that direction in the background, in both directions without end.

#### Scenario: Shown runs come first
- **WHEN** a user presses Prognose
- **THEN** the 6 runs shown and their forecasts appear without waiting for the runs around them,
  which are loaded and forecast afterwards

#### Scenario: A step within loaded runs is immediate
- **WHEN** the 12 runs after the window are loaded and the user presses Later
- **THEN** the next 6 runs appear without a new request

#### Scenario: Two steps trigger the next chunk
- **WHEN** after the first result the user presses Later until the window is 6 runs from the last
  loaded run that way
- **THEN** the next 12 later runs are requested and forecast in the background, and the window
  keeps responding while they load

### Requirement: Earlier and Later sit beside the entries on wide screens
From tablet width up, the Earlier and Later buttons SHALL sit in one column to the left of the
entries, Earlier above Later, together as tall as the entries; on a phone they SHALL sit in one
row below the entries.

#### Scenario: Desktop
- **WHEN** a result is shown on a desktop or tablet screen
- **THEN** both buttons are in one column to the left of the entries, Earlier on top

#### Scenario: Phone
- **WHEN** a result is shown on a phone
- **THEN** both buttons are in one row below the entries

## ADDED Requirements

### Requirement: The Prognose card steps through runs six at a time, without end
The Prognose card SHALL show 6 runs at the chosen stop at a time: at first the 2 at or before
the chosen time and the 4 after it. Each press of Earlier or Later SHALL move the window by 6
runs. The window SHALL have no end: after a day's last run come the next day's runs, before its
first come the previous day's, however few runs a day the stop has. Both buttons SHALL always be
available once a result is shown, except while a slide is running.

#### Scenario: First result
- **WHEN** a user presses Prognose for 08:00 at a stop served every 30 minutes from 07:20
- **THEN** the card shows the runs at 07:20, 07:50, 08:20, 08:50, 09:20 and 09:50

#### Scenario: Later moves by six runs
- **WHEN** the card shows the runs at positions 1 to 6 and the user presses Later
- **THEN** the card shows the runs at positions 7 to 12

#### Scenario: A stop with few runs goes on into other days
- **WHEN** the stop has 4 runs a day and the user presses Later repeatedly
- **THEN** each press shows the next 6 runs, continuing into the following days, and the button
  never becomes unavailable

#### Scenario: Earlier goes into the day before
- **WHEN** the user presses Earlier past the first run of the chosen day
- **THEN** the card shows the previous day's last runs, each with its own date


### Requirement: The lookup shows the chosen run's route beside the list
Under the search form, the view SHALL show the list (Prognose runs or a station board) and the
chosen run's route as entries of the same layout: side by side from 1024 px wide, the route
under the list below that. Choosing an entry SHALL show its route without leaving the view. The
first entry at or after the chosen time SHALL be chosen when the result appears.

#### Scenario: Choosing a run
- **WHEN** a user presses Prognose and then chooses the second run
- **THEN** the route beside the list changes to that run, the run is marked as chosen, and the
  address and scroll position stay the same

#### Scenario: Nothing chosen yet
- **WHEN** a station board for 11:00 appears
- **THEN** the route of its first call at or after 11:00 is already shown

#### Scenario: Phone
- **WHEN** a user on a 390 px screen chooses a run
- **THEN** the page scrolls to the route under the list

### Requirement: The route takes the list's height and scrolls
From 1024 px wide, the list SHALL take 7 parts of the width and the route 5, and the route's card
SHALL be exactly as tall as the list's card. A station board SHALL be 40rem tall (36rem below
1024 px), and the route 36rem below 1024 px. Calls and stops that do not fit SHALL scroll inside
their card. A board SHALL start at its first call at or after its moment. A route SHALL start at
the user's stop.

#### Scenario: Long board, short route
- **WHEN** a board of 60 calls is shown beside a route of 4 stops
- **THEN** both cards have the same height, and only the board's calls scroll

#### Scenario: Long route beside the Prognose card
- **WHEN** a route of 30 stops is chosen at its 20th stop
- **THEN** the route's card is as tall as the Prognose card and its stops are scrolled to the 20th

### Requirement: A route's stops show no chevron
The route's stops SHALL be entries without the chevron at their end, with the three columns
taking its place, and SHALL stay clickable.

#### Scenario: Route beside the list
- **WHEN** a route is shown beside the list
- **THEN** its stops have no chevron and choosing one still opens that station's board

### Requirement: The content uses up to 55 percent of a wide window
Every v2 view SHALL be at most as wide as the larger of 64rem and 55 % of the window.

#### Scenario: Full HD window
- **WHEN** a view is shown in a 1920 px wide window
- **THEN** its content is 1056 px wide, centred

### Requirement: A stop of the route opens its board in place
On the lookup view, choosing a stop of the route SHALL replace the list and the route with that
station's board, as wide as both and as tall. The board SHALL be for the run's day and its time
at the stop, with the run's call marked and scrolled to. A Back button SHALL restore the list and
the route as they were.

#### Scenario: Open and return
- **WHEN** a user chooses Krefeld-Oppum in the route of RE10 82270 and then presses Back
- **THEN** Krefeld-Oppum's board for that day from 12:59 is shown with RE10 82270 marked, and
  Back shows the same runs and the same chosen route again

### Requirement: The route is forecast for the run's day
The train route endpoint SHALL take an optional `date` (YYYY-MM-DD) and forecast the stops for
that day, today without it, and answer 400 for any other value. The lookup's route and a train
page with `?date=` SHALL pass the run's day, so a stop's delay matches the list's entry.

#### Scenario: Same numbers
- **WHEN** a board for 2026-10-16 shows ICE 517 at Bochum Hbf as +8 min, expected 10:55
- **THEN** the route of that call shows Bochum Hbf as +8 min, expected 10:55

#### Scenario: Bad date
- **WHEN** a client requests a route with `date=16.10.2026`
- **THEN** the answer is 400 with an error saying the date must be YYYY-MM-DD

### Requirement: The route's header names the train in plain words
The route's header SHALL show the destination, the train's name and number, the run's day, the
number of stops and a link to its line named "Line <name>" (German "Linie <name>"). It SHALL NOT
show raw category or operator codes.

#### Scenario: Regional train
- **WHEN** the route of RE10 82270 on 2026-10-16 is shown
- **THEN** its header reads "Fri 16/10", "4 stops" and "Line RE10", and no "RRB · TDRR"

## REMOVED Requirements

### Requirement: The Prognose card steps through runs three at a time, without end
**Reason**: The card now shows and steps by 6 runs, so it fills the height it shares with the
route beside it.
**Migration**: See "The Prognose card steps through runs six at a time, without end".
