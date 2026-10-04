# Spec Delta

## Purpose

Fetches, stores and serves weather forecasts on the server for the delay model, so every
prediction can use the forecast without users' browsers calling a weather service, and keeps an
archive of past forecasts for training.

## ADDED Requirements

### Requirement: Forecasts are fetched by the server and shared
Weather forecasts SHALL be fetched only by the server, on a schedule, never per page view or per
user. Each refresh SHALL cover every grid cell that contains a tracked station, hourly for at
least the next 7 days, with temperature, precipitation, snowfall and maximum wind gust. The
weather service usage per day SHALL be independent of the number of users. Live refreshes SHALL
use at most 10% of the weather service's free daily allowance, counted the way the service
counts it (for Open-Meteo: one unit per location per request).

#### Scenario: Many users, same requests
- **WHEN** 10,000 users open station boards in one hour
- **THEN** the number of requests to the weather service in that hour is the same as with one
  user

#### Scenario: A refresh covers every tracked station
- **WHEN** a refresh completes
- **THEN** every tracked station maps to a grid cell with hourly forecast values for the next 7
  days

### Requirement: Stored forecasts can be looked up as of a moment
Every stored forecast value SHALL record when the forecast was issued. A lookup for a station, an
hour and a moment SHALL return the latest value issued at or before that moment, or nothing if
none exists. Values SHALL NOT be overwritten by later forecasts; they are kept alongside them.
Older stored forecasts MAY be thinned, but for every past hour the latest value issued at least
one day earlier SHALL be kept for at least the length of the training history.

#### Scenario: Thinning keeps the day-ahead value
- **WHEN** forecasts for an hour three weeks ago were issued 48, 30, 24 and 6 hours before it,
  and thinning has run
- **THEN** the value issued 24 hours before is still stored

#### Scenario: Two forecasts for the same hour
- **WHEN** forecasts for Tuesday 17:00 were issued on Monday 06:00 and on Tuesday 06:00, and the
  lookup moment is Tuesday 05:00
- **THEN** the lookup returns Monday 06:00's value

### Requirement: An archive of past forecasts is available for training
The server SHALL be able to download past forecasts for every grid cell over the whole training
history, as issued at least one day before each hour, and store them in the same form as live
forecasts. The download SHALL be resumable and SHALL skip what is already stored.

#### Scenario: The archive covers the training history
- **WHEN** the archive download finishes for the dataset's date range
- **THEN** every grid cell has a forecast value, issued at least a day earlier, for every hour of
  that range that the weather service provides, and the command reports the hours it could not
  get

#### Scenario: Resuming the download
- **WHEN** the download is interrupted and run again
- **THEN** it requests only the cells and dates not yet stored

### Requirement: A failing weather service degrades to missing values
If a refresh fails, the server SHALL keep serving predictions, using the stored forecasts that
still cover the requested hours. Hours without a forecast issued before the cutoff SHALL produce
missing weather features. The failure SHALL be logged with the time of the last successful
refresh.

#### Scenario: The weather service is down for a day
- **WHEN** refreshes fail for 24 hours
- **THEN** station boards still show model predictions, and calls whose hours are beyond the last
  stored forecast have missing weather features

### Requirement: The weather source is credited
The site SHALL credit the weather data source as its licence requires, visibly on every view
that shows model predictions, with a link to the source.

#### Scenario: Attribution is visible
- **WHEN** a user opens any station board, train or line page
- **THEN** the page shows the weather source's name and licence with a link to the source
