# Proposal

## Why

In the live accuracy test, about a quarter of arrivals missed the real delay by more than
3 minutes. Most of these misses are trains that were really late, and today the model can't see
anything that would warn it: its inputs are the timetable, calendar and station data only. Some
of those delays are known before the train runs:
- construction works, which DB announces days ahead
- a train that has been late every day this week
- a storm in the forecast

Adding these inputs before the full-year model is trained (change
`rebuild-arrival-model-for-full-year`, group 5) goes after the misses that can be predicted.
Separately, the app should show the likely range next to the estimate, so an uncertain call
looks uncertain.

Live delays of trains already running are out of scope. That is a much larger structural change
and is postponed.

## What Changes

- **Construction and disruption notices as inputs.** For each call, the number of construction
  notices and disruption notices on its trip (and at its stop) that were already published at the
  forecast time. The data is already collected into `tt_message`. For measured timing: 99% of
  construction notices exist before the service day starts and 84% a day ahead. Trips with a
  notice were more than 5 minutes late on 21.4% of stops, against 17.2% without one
  (14–20 Sep 2026).
- **Recent delays as inputs.** This train's average arrival delay at this station over the last
  7 days, and the same for its line at this station. Only runs already finalized at the forecast
  time are counted, and the run counts are included. Computed from the project's own database.
- **Weather forecasts as inputs.** The forecast temperature, precipitation, snowfall and wind
  gusts at the call's station and scheduled hour. The server fetches forecasts from Open-Meteo
  a few times a day for a coarse grid over Germany, stores them and reuses them for every call
  and user. The number of API calls doesn't depend on the number of users. Training uses
  Open-Meteo's archive of past forecasts, so the model learns from the kind of data it will get,
  never from the weather that actually happened.
- **Unknown stays unknown.** When an input isn't available at prediction time (a date beyond the
  weather forecast's reach, a trip whose plan isn't imported yet, a train with no recent runs),
  it is marked missing, never zero, and training includes such rows.
- **BREAKING (spec):** the delay-model rule "features come from the timetable, calendar and
  station data only; no disruption messages" is replaced by a rule about time: every input must
  have been known before the forecast time, and each input's source is named. Deployed artifacts
  keep working unchanged, because they declare their own input lists.
- **The full-year search measures the new inputs.** One more ablation trial (the best network
  without the new inputs). The report adds a breakdown by "has a construction notice" and puts
  the share of arrivals missed by more than 3 and by more than 5 minutes up front, since that is
  the figure this change targets.
- **Rounded figures and likely range in the app.** The model's estimate is shown in whole
  minutes like the stored average, with its 25th–75th percentile range next to it, for example
  "ML +1 min (+0 … +4)", not only in a tooltip.

## Capabilities

### New Capabilities
- `weather-forecasts`: fetching, storing and serving weather forecasts server-side for the delay
  model, including the archive of past forecasts used for training and the limits on external
  API calls.

### Modified Capabilities
- `delay-model`: the "knowable before the train runs" requirement becomes a time rule with named
  sources. New requirements cover the notice, recent-delay and weather inputs, missing inputs,
  and reporting the share of large misses.
- `web-frontend-v2`: the model prediction is shown in whole minutes, with its likely range, not
  only the point figure.

## Impact

- **Code:**
  - `src/dbrel/ml/features.py`, `full_history.py`, `full_year.py`, `full_year_search.py`,
    `dataset.py`, `timetable.py`
  - `src/dbrel/web/queries.py` (`_model_input` gains the new inputs)
  - a new `src/dbrel/weather/` package, with a CLI command to fetch the forecast archive and a
    refresh step in `collect`
  - `bahn-buddy-predict/src/components/v2/ModelDelta.tsx`
- **Database:**
  - new tables for weather forecasts and for daily per-train/per-line delay aggregates, created
    by the existing schema migration
  - `tt_message` is only read
- **External dependency:** Open-Meteo, free non-commercial tier, no API key; forecast and
  previous-runs endpoints. A few dozen requests a day, independent of user numbers, plus a
  one-off archive download for training.
- **Ordering:** this should land before `rebuild-arrival-model-for-full-year` group 5 (the
  training runs), which then trains with the new inputs.
- **Deployed models:** the served TabM `20260924T183944Z` and the window MLP are unaffected.
