## Purpose

Plans train journeys between two stations and ranks them by the delay model's forecast arrival
time instead of the timetable, so travellers see which connections will really work and when
they will really arrive.

## ADDED Requirements

### Requirement: A plan is requested with two stations, a date and a time
The system SHALL answer `GET /api/plan?from=<station id>&to=<station id>&date=YYYY-MM-DD&time=HH:MM`.
The date and time are the earliest local departure (Europe/Berlin; on the autumn day the first
of the two repeated hours). Missing or unparsable parameters, or the same station twice, SHALL
give status 400 naming the problem. An unknown station id SHALL give status 404.

#### Scenario: A valid request
- **WHEN** a client requests `/api/plan?from=269&to=501&date=2026-10-20&time=08:00`
- **THEN** the response has status 200 and lists connections leaving station 269 at or after
  08:00 on 2026-10-20

#### Scenario: Time missing
- **WHEN** a client requests `/api/plan?from=269&to=501&date=2026-10-20`
- **THEN** the response has status 400 and its `error` says that time must be HH:MM

#### Scenario: Same station twice
- **WHEN** `from` and `to` are both 269
- **THEN** the response has status 400 and its `error` says the stations must differ

#### Scenario: Unknown station
- **WHEN** `to` is a station id that does not exist
- **THEN** the response has status 404 and its `error` names that id

### Requirement: A ride's arrival is its forecast arrival
For every train ride in a search, the arrival time at the stop where the traveller gets off
SHALL be the scheduled arrival plus the current model's median arrival delay at that stop. A
negative median SHALL count as zero. When the model gives no forecast for a stop that has a
scheduled arrival, the scheduled arrival SHALL be used and the leg SHALL report no forecast.

#### Scenario: A late train
- **WHEN** a train is scheduled to arrive at Dortmund Hbf at 09:10 and the model's median there
  is 4 minutes
- **THEN** the search treats the ride as arriving at 09:14

#### Scenario: An early forecast
- **WHEN** the model's median for a stop is -2 minutes
- **THEN** the search treats the ride as arriving at the scheduled time

### Requirement: Connections are ranked by forecast arrival at the destination
The system SHALL search for the connection with the earliest forecast arrival at the destination,
not the earliest scheduled arrival. Ties SHALL be broken by fewer changes, then by later
departure.

#### Scenario: The forecast reverses the timetable's order
- **WHEN** route X is scheduled to arrive at 10:00 but is forecast at 10:12, and route Y is
  scheduled at 10:05 and forecast at 10:06
- **THEN** route Y is the first connection listed

### Requirement: A change needs time between the forecast arrival and the next departure
A change from one train to another at a station SHALL be possible only when the forecast arrival
of the first train plus 5 minutes is no later than the second train's expected departure (next
requirement). No change time SHALL apply at the start station, where trains are boarded at their
scheduled departure.

#### Scenario: A change made impossible by a forecast delay
- **WHEN** a train is scheduled into Hamm at 08:30, the model forecasts 4 minutes delay there, and
  the connecting train starts at Hamm and leaves at 08:36
- **THEN** that change is not used (08:34 + 5 min is after 08:36), and a later connecting train
  is used instead

#### Scenario: A change that still works
- **WHEN** the same arrival is forecast on time and the connecting train leaves at 08:36
- **THEN** the change is used (08:30 + 5 min is before 08:36)

#### Scenario: Boarding at the start
- **WHEN** the user asks to leave at 08:00 and a train leaves the start station at 08:00
- **THEN** that train can be boarded

### Requirement: A connecting train leaves late only if it arrives late
A connecting train's expected departure from a change station SHALL be its scheduled departure
plus its own nonnegative forecast arrival delay at that station, capped at 30 minutes.
Arrival forecasts SHALL remain uncapped. A train that is forecast on time there
leaves on time. A train that starts there, or has no forecast there, leaves on time. This is used
only to decide whether a change works and SHALL NOT be shown as a departure forecast.

#### Scenario: A late connecting train can be caught
- **WHEN** the first train is forecast into Hamm at 08:34, and the connecting train, scheduled
  to arrive at 08:35 and leave at 08:36, is itself forecast 4 minutes late at Hamm
- **THEN** its expected departure is 08:40 and the change is used (08:34 + 5 min is before 08:40)

#### Scenario: A punctual connecting train is not waited for
- **WHEN** the same connecting train is forecast on time at Hamm
- **THEN** it leaves at 08:36 and the change is not used

#### Scenario: Carried departure delay is capped
- **WHEN** a connecting train is scheduled to leave at 08:36 and its median arrival delay at
  that station is 45 minutes
- **THEN** its expected departure for the change rule is 09:06, while its forecast arrival
  still uses the full 45-minute delay

### Requirement: Only trains that run on the chosen day are used
The search SHALL use a train on a service date only if its running profile says it runs on that
day of the week. A public holiday in the boarding station's state SHALL count as a Sunday. Of a
train's route variants, only the one that ran most often on that day SHALL be used. Variants
that did not run in the profile's window SHALL NOT be used.

#### Scenario: A weekday-only train on a Sunday
- **WHEN** a train ran on every Monday to Friday of the profile window and never on a weekend,
  and the user plans a journey on a Sunday
- **THEN** no connection uses that train

#### Scenario: A holiday counts as Sunday
- **WHEN** the user plans a journey on 2026-10-03 (a Saturday that is a public holiday in all
  states)
- **THEN** only trains that run on Sundays are used

#### Scenario: A stale route variant
- **WHEN** a train's variant that calls at Berlin-Wedding last ran months before the profile
  window
- **THEN** no connection boards or leaves that train at Berlin-Wedding

### Requirement: A journey never rides through a faulty schedule step
A ride SHALL NOT continue past a stop whose scheduled time is earlier than the previous stop's
time, or more than 6 hours after it. Stops before the faulty step SHALL stay usable.

#### Scenario: A time that jumps a day
- **WHEN** a train's schedule has stop 7 at 05:03 and stop 8 at 28:57 the same service day
- **THEN** the search can use the train up to stop 7 and never to stop 8 or later

### Requirement: The search has fixed bounds
A search SHALL board at the start station only trains leaving within 6 hours of the requested
time. At a change station, it SHALL board only trains leaving within 3 hours of the forecast
arrival. It SHALL use at most 5 trains (4 changes). It SHALL ignore arrivals more than 16 hours
after the requested time.

#### Scenario: Night gap at the start
- **WHEN** the first train from the start station leaves 7 hours after the requested time
- **THEN** no connection is found, and the reason says that there is no connection

### Requirement: Connections are listed in the order they leave
The response SHALL list at most 4 connections, in the order they leave. The first is the one
that arrives soonest of those leaving at or after the requested time; each next one is the
soonest-arriving of those leaving at least a minute after the one before. So no listed connection
is beaten by a later-leaving one that arrives earlier, and no two leave at the same time.
Fewer than 4 SHALL be listed when no further connection is found within the bounds.

#### Scenario: Connections in order
- **WHEN** a user plans München Hbf to Stuttgart Hbf at 12:00 on a weekday
- **THEN** up to 4 connections are listed, each leaving later than the one before and arriving
  no sooner

#### Scenario: A slower train that leaves earlier is not listed
- **WHEN** a train leaves at 12:07 and arrives at 13:18, and another leaves at 12:12 and arrives
  at 13:15
- **THEN** only the one leaving at 12:12 is listed

#### Scenario: Only one way exists
- **WHEN** only one connection exists within the bounds
- **THEN** exactly one connection is listed

### Requirement: Earlier connections can be asked for
`GET /api/plan` SHALL accept `before=1`. The date and time then name the moment the connections
must leave before, and the response SHALL list, in the order they leave, the last (up to 4)
connections that leave before it, and set `before` to true. A connection SHALL be left out when it
arrives no sooner than the first connection leaving at or after that moment, since that one
leaves later and arrives as soon. The search SHALL look back one hour, then four, then six,
stopping as soon as it holds 4 connections.

#### Scenario: Stepping back
- **WHEN** a client asks for the connections before 17:28 and the page after them starts at 17:28
- **THEN** the connections listed leave before 17:28, and none arrives later than the first
  connection leaving at 17:28 does

#### Scenario: Nothing earlier
- **WHEN** every connection leaving before the moment is beaten by one leaving at or after it
- **THEN** no connection is listed and the reason is `no_connection`

### Requirement: Each listed connection leaves as late as possible
For each listed connection, the system SHALL list the instance of its route that arrives soonest
by the forecast. Among instances that arrive equally early, it SHALL list the one that leaves the
start station latest.

#### Scenario: An unneeded early start
- **WHEN** the best route can be started at 08:00 or at 08:30 with the same forecast arrival
  (the 08:00 start only waits longer at the change)
- **THEN** the connection is listed leaving at 08:30

### Requirement: Each connection describes its legs and changes
Each connection SHALL give its route key, its departure, its scheduled and forecast arrival, its
duration to the forecast arrival, its number of changes and every leg: train, stations, times,
forecast band and expected arrival. Each change SHALL give its planned minutes (scheduled arrival
to scheduled departure), minutes left (forecast arrival to the connecting train's expected
departure), its miss-chance band and whether it is at risk.

#### Scenario: A change at risk
- **WHEN** a leg's 75th-percentile forecast arrival plus 5 minutes is after the next train's
  expected departure
- **THEN** that change is marked at risk, even though the median makes it work

#### Scenario: A comfortable change
- **WHEN** a leg arrives at 08:30 with p25/p50/p75 of 0/1/2 minutes and the next train leaves at
  08:45
- **THEN** the change reports 15 planned minutes, 14 minutes left, the band `low` and not at risk

### Requirement: The chance of missing a change is given as a band
The model forecasts only the 25th, 50th and 75th percentile of the arrival delay, so the chance
of missing a change SHALL be one of four bands, never one percentage. With slack = the delay the
incoming train can have and still leave 5 minutes to change, the band SHALL be `low` (under
25 %) when the p75 delay fits the slack, `medium` (25–50 %) when only p50 fits, `high` (50–75 %)
when only p25 fits, and `very_high` (over 75 %) otherwise.

#### Scenario: Bands from the quantiles
- **WHEN** the incoming train's p25/p50/p75 delays are 2/5/8 minutes
- **THEN** the band is `low` with 10 minutes of slack, `medium` with 6, `high` with 3 and
  `very_high` with 1

### Requirement: Bands decide the risk mark and the connection's band
A change without a forecast SHALL have the band `null`. A change SHALL be at risk exactly when
its band is neither `low` nor `null`. A connection SHALL carry the worst band among its changes,
and `null` when it has no change or none of its changes has a band.

#### Scenario: The worst change sets the connection's band
- **WHEN** a connection has one change in `low` and one in `high`
- **THEN** the connection's band is `high`

#### Scenario: A direct connection
- **WHEN** a connection has no change
- **THEN** its band is `null`

### Requirement: The plan response names the model and the data's age
Every plan and timings response SHALL include the stamp of the model artifact that made the
forecasts and the newest service date in the running profile.

#### Scenario: Data age is shown
- **WHEN** the running profile's newest service date is 2026-09-28
- **THEN** the response's `data_until` is `2026-09-28`

### Requirement: No plan is made without a model
When no current model artifact exists, the plan and timings endpoints SHALL respond with status
503 and an error saying no delay model is available. They SHALL NOT plan with scheduled times
alone.

#### Scenario: No model
- **WHEN** a plan is requested on a server with no current model
- **THEN** the response has status 503 and lists no connections

### Requirement: An empty result says why
When no connection is listed, the response SHALL give a reason: `from_unserved` (no train stops
at the start station), `to_unserved` (none at the destination), `no_connection` (none within
the bounds) or `search_limit` (the search hit its work or time limit first).

#### Scenario: A station without trains
- **WHEN** the start station has no scheduled stops
- **THEN** the response has status 200, an empty list and the reason `from_unserved`

#### Scenario: A search that runs too long
- **WHEN** a search reaches its work or time limit before reaching the destination
- **THEN** the response has status 200, the connections found so far, and the reason
  `search_limit` if none was found

### Requirement: One route can be listed at other times
The system SHALL answer `GET /api/plan/timings?from&to&date&time&route=<route key>`. It SHALL
list the instances of that route whose first train leaves within 2 hours before or after the
given moment, sorted by departure, at most 24. Each instance SHALL be described like a listed
connection.

#### Scenario: Earlier and later times of one route
- **WHEN** a client asks for the timings of an RE 1 then ICE route around 08:30
- **THEN** the response lists instances leaving from 06:30 to 10:30, each boarding and leaving
  the same lines at the same stations

#### Scenario: A malformed route key
- **WHEN** the route key does not have the form the plan response uses
- **THEN** the response has status 400

### Requirement: Each instance takes the earliest-arriving connecting trains
In an instance, each later leg SHALL use the train of that leg's line that can be caught (by the
change rule) and has the earliest forecast arrival at the leg's alighting station. A first
train whose later legs cannot be caught within 3 hours SHALL give no instance. When two first
trains lead to the same later trains, only the later-leaving first train SHALL be listed.

#### Scenario: Two first trains lead to the same connection
- **WHEN** the 08:00 and the 08:20 RE both reach the change in time for the same 09:05 ICE
- **THEN** only the instance with the 08:20 RE is listed

### Requirement: The planner page asks for two stations, a date and a time
The frontend SHALL offer a planner view at `/plan` with a start station, a destination station,
a swap button, a date and a time. A Today button SHALL sit directly under the date field and a
Now button directly under the time field. The search button SHALL be
disabled until both stations, the date and the time are set and the stations differ. Nothing
SHALL be requested before the button is pressed. Changing any input SHALL withdraw shown results.

#### Scenario: The button waits for every input
- **WHEN** a user has chosen Köln Hbf as the start and a date and time, but no destination
- **THEN** the search button is disabled and no plan request has been sent

#### Scenario: Station pickers list stations only
- **WHEN** a user types "Dortm" in either station field
- **THEN** the suggestions list only stations, never lines or trains

### Requirement: Station pickers offer the stations used most
Each search SHALL count its start and destination station in the browser's local storage, on
this device only, keeping the 30 most used. An empty station field SHALL, when focused, list up
to 6 of them, most used first, under the heading "Häufig genutzt" / "Often used". While the user
types, up to 3 of them whose name contains the typed text SHALL come first, followed by the
other suggestions without repeats. When local storage is unavailable the pickers SHALL work as
before, with no list.

#### Scenario: An empty field lists the stations used most
- **WHEN** a user has searched Bochum Hbf → Bremen Hbf three times and Köln Hbf → Bochum Hbf once,
  and focuses an empty station field
- **THEN** the list shows Bochum Hbf, Bremen Hbf, Köln Hbf, in that order

#### Scenario: A used station leads the suggestions
- **WHEN** Bochum Hbf has been searched before and the user types "Bo"
- **THEN** Bochum Hbf is the first suggestion, even if the server does not return it for "Bo"

#### Scenario: Editing withdraws results
- **WHEN** results are shown and the user changes the time
- **THEN** the results disappear until the button is pressed again

### Requirement: The planner page lists the connections
After the button is pressed, the page SHALL show up to 4 connections, in the response's order.
Each entry SHALL show, in three columns: the scheduled departure → arrival with its trains and
duration under it; the forecast arrival with its delay ("+22 Min.", or "pünktlich" / "on time")
under it; and the connection's miss-chance band with the change stations under it, or "kein
Umstieg" / "no change" for a direct connection.

#### Scenario: A connection with a risky change
- **WHEN** a listed connection's worst change has the band `medium`
- **THEN** its risk column shows a warning icon and "25–50 %" in amber

#### Scenario: A safe connection
- **WHEN** a listed connection's band is `low`
- **THEN** its risk column shows "unter 25 %" / "under 25%" in teal

#### Scenario: No connection
- **WHEN** the response has no connections and the reason `no_connection`
- **THEN** the page says that no connection was found within the search limits

### Requirement: Journey lists name their columns
The connections list SHALL have a header naming its three columns: the trip by the timetable, the
forecast arrival, and the risk of missing a change. Below tablet width the header SHALL use short
names: Fahrplan / Prognose / Risiko, Timetable / Forecast / Risk.

#### Scenario: Headers on a phone
- **WHEN** the connections list is shown at 390 px width in German
- **THEN** its header reads Fahrplan, Prognose, Risiko, and no header or band is cut off

### Requirement: Choosing a connection shows its journey beside the list
The planner results SHALL be laid out like the train page: the connections card on the left, the
chosen connection's journey details on the right, from desktop width up; under the card on a
phone and tablet. The first connection SHALL be chosen when the results appear. Clicking another
connection SHALL mark it as chosen and swap the details beside the list, and SHALL NOT scroll the
page. No card for the same route at other times SHALL be shown.

#### Scenario: Choosing a connection
- **WHEN** a user clicks the second connection
- **THEN** it is marked as chosen, and the card on the right shows that connection's trains and
  changes

### Requirement: Earlier and Later page through the connections
The connections card SHALL have Earlier and Later buttons on its left from tablet width up and
below its list on a phone, as the train page's Prognose card has. Later SHALL show the
connections leaving from a minute after the last one listed; Earlier SHALL show those leaving
before the first one listed, by asking with `before=1`. With no connection listed, Earlier and
Later SHALL move the search by the length of its reach (6 hours) from where it stood. Pressing
either SHALL put the new moment, and `before` for Earlier, in the page address, and SHALL reset
the chosen connection to the first.

#### Scenario: Later
- **WHEN** the list ends with a connection leaving at 14:20 and the user presses Later
- **THEN** the address holds the time 14:21 and the list shows the connections leaving from then

#### Scenario: Earlier
- **WHEN** the list starts with a connection leaving at 17:28 and the user presses Earlier
- **THEN** the address holds the time 17:28 with `before`, the heading reads "vor 17:28" /
  "before 17:28", and the list shows the last connections leaving before 17:28

### Requirement: Journey details show the legs and changes
The details card SHALL show, in its header, the start and destination stations, then the day,
the timetabled departure → arrival, the forecast arrival, the duration, the number of changes, and
a line saying how to read the card. A leg's train SHALL link to its train page, highlighting the
boarding station. It SHALL stay in view while the page scrolls past a long connections card, from
desktop width up.

#### Scenario: Following a leg's train
- **WHEN** a user clicks the train of a leg that boards at Hamm (Westf) Hbf
- **THEN** the train page opens with Hamm (Westf) Hbf highlighted as the user's stop

### Requirement: Journey details show each train and each change as a block
Each train SHALL be a block: the train with its number and direction, then its departure time and
boarding station, then its arrival time and alighting station with "Prognose 11:45 (+4 Min.)" /
"Forecast 11:45 (+4 min)". Between two trains a change block SHALL show the station, the time to
change by the timetable and by the forecast, and the miss-chance band.

#### Scenario: Reading a change
- **WHEN** a change at Gelsenkirchen Hbf has 9 planned minutes, 6 minutes left and the band
  `medium`
- **THEN** its block reads "Umstiegszeit: 9 Min. laut Fahrplan, ca. 6 Min. laut Prognose" and,
  under "Risiko, den Anschluss zu verpassen", "25–50 %"

### Requirement: Every connection entry keeps one fixed height
Every entry in the connections list SHALL have the same height in every case: the chosen one,
another one, one with a risky change, one whose arrival falls on the next day. This SHALL hold at
desktop and phone width. An entry SHALL NOT grow to show its legs. The legs are shown in their
own card.

#### Scenario: Heights are equal
- **WHEN** the list shows a normal entry, a risky entry and an after-midnight entry, at
  1280 px and at 390 px width
- **THEN** all entries have the same height

### Requirement: The planner keeps its inputs in the address
After the button is pressed, the page address SHALL hold the start, destination, date, time,
`before` when Earlier was pressed, and the chosen connection. Opening that address SHALL show the
same inputs and results. Going back from a train page SHALL return to the same results.

#### Scenario: Reloading the page
- **WHEN** a user reloads `/plan?from=269&to=501&date=2026-10-20&time=08:00&option=1`
- **THEN** the same inputs are filled in, the plan is requested, and the second connection is
  shown as chosen

### Requirement: The planner opens on the current date and time
When the address holds no date or time, the planner's date and time fields SHALL show the current
Berlin date and time. They SHALL be filled in after the page has loaded in the browser, not in
the server-rendered page. A date or time in the address SHALL win.

#### Scenario: Opening the planner
- **WHEN** a user opens `/plan` at 16:44 on 2026-10-21
- **THEN** the date field shows 2026-10-21 and the time field 16:44

### Requirement: The planner shows progress and failures plainly
While a plan is loading, the page SHALL show placeholder entries and say that the search can
take up to 20 seconds. A failed request SHALL show the error text. A 503 SHALL say that no
forecast model is available.

#### Scenario: Slow search
- **WHEN** the plan request has not answered yet
- **THEN** three placeholder entries are shown with the waiting note

#### Scenario: Server error
- **WHEN** the plan request fails with status 500
- **THEN** the page shows the general error message and no connections
