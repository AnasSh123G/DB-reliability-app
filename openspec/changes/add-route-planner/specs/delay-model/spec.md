## ADDED Requirements

### Requirement: The journey planner forecasts on the server with the current artifact
The web server SHALL run the artifact `CURRENT` points to only to answer journey-planner
requests. Model inputs SHALL be encoded by the same code and vocabulary as the browser's inputs,
with the same route variant, service date and trip start. Every other view SHALL keep sending
inputs to the browser, which runs the model itself. Serving the model's files SHALL stay
inference-free.

#### Scenario: A planner forecast equals the browser's
- **WHEN** a planner leg alights at a stop, and the same train, route variant, station and service
  date is forecast in the browser from the encoded input the server sends for that call
- **THEN** the two medians differ by at most 0.01 minutes

#### Scenario: Other views are unchanged
- **WHEN** a station board, train route or Prognose card is loaded
- **THEN** the server returns encoded inputs and runs no inference for that request

#### Scenario: CURRENT moves
- **WHEN** `CURRENT` is pointed at a different artifact while the server is running
- **THEN** the next planner request forecasts with the new artifact and reports its stamp, and
  no forecast cached under the old stamp is reused

### Requirement: Planner forecasts use only timetable-time facts
Planner forecasts SHALL use the same inputs as every other forecast: timetable, calendar,
station data and stored notice rates. They SHALL NOT use observed delays, trips or averages.
Each stop SHALL be forecast on its own, without conditioning on the forecast at an earlier stop.

#### Scenario: Works without observations
- **WHEN** the database has no trip or trip_stop rows (the deployed database)
- **THEN** planner forecasts are produced from the timetable and stored notice rates alone

### Requirement: The planner asks the model for arrivals only
The planner MAY carry a train's forecast arrival delay at a stop over to that train's departure
from the same stop, capped at 30 minutes, to judge whether a change works. Arrival forecasts
SHALL remain uncapped. It SHALL NOT show that as a departure
forecast, and SHALL NOT ask the model for one.

#### Scenario: A departure is never forecast
- **WHEN** the planner judges a change onto a train that is forecast 4 minutes late arriving
- **THEN** the model is asked only for that train's arrival, and no departure forecast appears in
  any response
