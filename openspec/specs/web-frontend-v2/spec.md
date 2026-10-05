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

It SHALL add no behaviour the current interface lacks and drop none it has. Its visual
appearance is governed by "The second interface has its own visual identity", not by this
requirement.

#### Scenario: Requests are identical
- **WHEN** a user performs the same sequence of actions in both interfaces (typing a query,
  choosing a suggestion, choosing direction, stop, date and time, pressing Prognose, opening a
  train, a station or a line, widening an empty board)
- **THEN** both interfaces send the same API requests, with the same endpoints and parameters,
  in the same order

#### Scenario: Behaviour is identical
- **WHEN** a user presses a key, clicks, or navigates back in the second interface
- **THEN** the result is the same as the same input in the current interface. This covers
  Prognose staying disabled until all five inputs are chosen, the suggestion list closing on an
  outside click, the search being kept on return within the visit and reset by a full reload,
  and "no average" shown where there is no average.

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
