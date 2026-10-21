## MODIFIED Requirements

### Requirement: The second interface is the default, with the same four views
The frontend SHALL serve the second interface at the root: a search view at `/`, a train route
view at `/train/<train id>`, a station board at `/station/<station id>` and a line detail view at
`/line/<line id>`. It SHALL also serve a journey planner at `/plan`, a view only the second
interface has (see the `route-planner` capability).

#### Scenario: Each view is served at the root
- **WHEN** a user opens `/`, `/train/2259`, `/station/5`, `/line/9` or `/plan`
- **THEN** the second interface's search, train route, station board, line detail or planner view
  is shown

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
the shared width, the logo row, the highlighted stop on the train page, the journey planner at
`/plan` and the header link to it. Its visual appearance is governed by "The second interface
has its own visual identity", not by this requirement.

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

#### Scenario: The planner exists only in the second interface
- **WHEN** a user opens `/v1/plan`
- **THEN** the app's existing "page not found" view is shown, as for any other unknown address

## ADDED Requirements

### Requirement: The header links to the journey planner
Every second-interface view SHALL show a link to `/plan` in the header row, between the logo and
the theme and language toggles. It SHALL have an icon and the label "Reiseplaner" in German or
"Journey planner" in English. On the planner view it SHALL be marked as the current page. On
a phone, it SHALL show the icon only, with the label as its accessible name.

#### Scenario: Opening the planner from a train page
- **WHEN** a user on `/train/2259` presses the planner link
- **THEN** `/plan` opens with empty stations and the date and time set to now

#### Scenario: Phone width
- **WHEN** the header is shown at 360 px width
- **THEN** the logo, the planner icon and both toggles stay on one row, and the planner link's
  accessible name is "Reiseplaner" or "Journey planner"

### Requirement: The train and station search opens on the current date and time
The date and time fields of the train and station search SHALL show the browser's current date and
time when the page opens, and keep a value already chosen in the same visit. They SHALL be filled
in after the page has loaded in the browser, not in the server-rendered page. A date and time in the
address of a station board SHALL win.

#### Scenario: Opening the search
- **WHEN** a user opens `/` at 16:44 on 2026-10-21
- **THEN** the date field shows 2026-10-21 and the time field 16:44
