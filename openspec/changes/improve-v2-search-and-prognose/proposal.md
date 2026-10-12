# Proposal

## Why

The v2 search page works, but it is hard to read and wasteful. Choosing a station jumps straight
to a live board, so you cannot see a station at another date or time. A Prognose card says
"Starts here" in a small badge and nothing else, so the missing forecast looks like a bug. The
cards on the page have different widths. Delays are told mostly through colour. Every run shows
the internal operational number ("RB 10478") instead of the name the user searched for. Each
"Earlier"/"Later" press waits for a new request. On the train page, nothing marks the stop the
user came from.

## What Changes

- **Station from the search page.** Choosing a station suggestion keeps the user on the search
  page. It fills the search field, and direction and stop are shown greyed out. The user picks a
  date and a time and presses the button, which opens the station board at that date and time.
  Today a station suggestion opens the live board at once. **BREAKING** for that behaviour.
- **Station board at a chosen date and time.** The board API takes an optional `date` and
  `time` and shows the window around that moment instead of around now. Without them it works as
  today: a live board that refreshes every minute.
- **Clear "starts here" and "ends here" notes.** In the Prognose card, a run that starts at the
  chosen stop shows a clearly visible note saying it starts here and so has no arrival to
  forecast, and gives its departure time. A run that ends at the chosen stop shows an equally
  visible "ends here" note. The station board shows the same notes.
- **Icons.** Every field label and value type gets a lucide icon, used the same way across
  search, Prognose, train page and board: state, search, direction, stop/station, date, time,
  arrival, departure, delay, and the "starts/ends here" notes.
- **One width, readable rows.** The search card, the hint, the Prognose card, the train page and
  the board all share one content width. Each Prognose run is laid out with labelled parts:
  scheduled arrival, forecast delay, expected arrival. The delay is written as text ("+4 min",
  "on time") with an icon, so colour is never the only signal.
- **Faster Earlier/Later.** The first request returns the 3 runs shown, and they are forecast
  first. The next request fetches the 12 runs before and the 12 runs after and forecasts them in
  the background. Each Earlier/Later press moves the window by 3 runs. When the window is within
  6 runs of the end of what has been loaded in that direction, the next 12 runs that way are
  fetched and forecast in the background. **BREAKING** for the current step of one run per
  press.
- **The searched name, shown once.** The Prognose card shows the name the user picked in search
  (for example "RE1" or "ICE 612") once, in its header. The runs no longer repeat a train name
  each, and the operational number ("RB 10478") is no longer shown in the Prognose card.
- **Logo row.** The logo and "BahnPredict" share one row with the theme and language toggles:
  logo on the left, toggles on the right.
- **Highlight the stop the user came from.** When a run is opened from the Prognose card or the
  board, the train page highlights that stop in its route, on desktop and on phone.

## Capabilities

### New Capabilities

None. Each new behaviour belongs to a view that an existing capability already covers.

### Modified Capabilities

- `web-frontend`: a station chosen in search goes through date, time and the button instead of
  opening the live board at once. The board can be shown for a chosen date and time. The
  Prognose gate covers the station path, which needs only date and time.
- `web-frontend-v2`: the requirement that v2 behaves exactly like v1 gets an explicit list of
  the differences already shipped and the ones added here. The new requirements cover the
  Prognose card (window loading and stepping, header name, starts/ends-here notes, readable delay
  text), the icons, the shared width, the logo row and the highlighted stop.

## Impact

- **Frontend (`bahn-buddy-predict/src`):** `components/v2/SearchV2.tsx` changes the most:
  station flow, run buffer, header and row layout. Also changed: `ShellV2.tsx` (logo row),
  `TrainV2.tsx` (highlighted stop), `BoardV2.tsx` (date and time, notes, icons, width),
  `ModelDelta.tsx` (delay as text with an icon), `hooks/use-dbrel.ts` and `lib/api.ts` (chunked
  trips-around requests, board date and time), `lib/i18n.ts` (new wording in DE and EN),
  `routes/station/$stationId.tsx` (`date`/`time` search params) and `routes/train/$trainId.tsx`
  (width).
- **Backend (`src/dbrel/web`):** `views.station_board` reads optional `date`/`time` and turns
  them into the `now` that `queries.station_board` already takes. The trips-around endpoint
  gets an offset-based chunk request (`offset`, `count`), so later chunks are fetched without
  fetching the runs already loaded again. The `before`/`after` parameters keep working.
- **v1 (`/v1`):** unchanged.
- **Ordering:** this change sits on top of the uncommitted Bundesland filter in the working
  tree (`lib/states.ts`, `tests/test_search_state.py`, the `state` parameter of
  `/api/suggest`). That work should be committed first.
- **Tests:** new backend tests for the board at a chosen date and time and for chunked
  trips-around. Everything is checked on the local server before any push. A push to main
  deploys to Fly.
