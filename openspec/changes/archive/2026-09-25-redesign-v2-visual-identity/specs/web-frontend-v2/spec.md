# Spec Delta

## REMOVED Requirements

### Requirement: The second interface works exactly like the current one
**Reason**: Pixel-for-pixel appearance parity with the current interface no longer applies now
that the second interface has its own visual identity (see proposal.md). This requirement
bundled appearance parity with behavioural and data parity; the two are split apart below so
appearance can change while behaviour and data stay exactly as they are.
**Migration**: No data or API migration. The behavioural and data parity this requirement also
covered is restated, unchanged in substance, by "The second interface behaves and communicates
exactly like the current one" below. Appearance parity is replaced by "The second interface has
its own visual identity" below.

## ADDED Requirements

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
