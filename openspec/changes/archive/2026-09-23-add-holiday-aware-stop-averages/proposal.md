# Proposal

## Why

Every stop in `trip_stop` now carries a graded delay (`arr_delay_s`/`dep_delay_s`) for all
~5,400+ tracked national stations, once the 30-day archive backfill lands. There is currently
no way to ask "how late does RE1 usually run through station X, in direction A, on the 11:00
departure" — that requires pooling many dated trips into one average, and doing it wrong (mixing
directions, mixing distinct daily departures of the same line, or mixing holiday timetables into
the weekday figure) produces a number that looks precise but describes nothing real.

This reverses the 2026-09-21 decision to delete the old average tables (`train_station_delay`,
`train_stop_average`) in favor of live-scored ML inference — confirmed directly with Anas on
2026-09-22. The ML redesign is unrelated and still pending; this is a return to materialized,
queryable averages, not a step in that redesign.

The existing holiday support (`holidays_de.py`) only covers Nordrhein-Westfalen, hand-rolled,
because tracking used to be Ruhr-only. National tracking means a holiday in Bavaria is not a
holiday in Hamburg, and the current `public_holiday` table (one row per date, no state) cannot
represent that.

## What Changes

- **BREAKING**: `public_holiday` gains a `state` dimension. A holiday no longer applies
  nationwide by default — Sundays still do, but a Bundesland-specific holiday (e.g.
  Fronleichnam) only applies to stations in that state. Existing rows (NRW-only) are
  superseded, not merged.
- Add `Station.state`: the German Bundesland (16 values) each station belongs to, derived once
  from `(lat, lon)`.
- Replace the NRW-only hand-rolled `holidays_de.py` ruleset with holiday dates fetched per state
  from an external public-holiday source, covering all 16 Bundesländer.
- Add a new materialized average table, keyed by
  `(station_id, line_id, direction, scheduled_time_of_day, holiday_flag)`, holding the mean (and
  sample count) of `arr_delay_s` and `dep_delay_s` over `RECORDED`-quality stops only.
- Add a CLI command to (re)build the averages table from `trip_stop`, on demand — not scored
  live, not recomputed per request.
- Implementation is split across three roles for cost control: a cheap fetch-only step
  (re-run the existing archive backfill), a cheap fetch-only step (pull holiday dates per
  state), and one step that does the actual schema/aggregation work. See design.md.

## Capabilities

### New Capabilities
- `holiday-calendar`: German public holidays resolved per Bundesland, replacing the NRW-only
  hand-rolled rules; a station is on a holiday timetable only for holidays that apply to its
  own state (plus Sundays, which apply everywhere).
- `stop-delay-averages`: a materialized table of mean arrival/departure delay per station, per
  line, per direction, per scheduled time-of-day, split by holiday vs. non-holiday, built from
  `RECORDED`-quality `trip_stop` rows.

### Modified Capabilities
(none — `holidays_de.py` and `public_holiday` have no existing OpenSpec capability; this
change establishes their spec for the first time rather than amending one.)

## Impact

- **Schema**: `public_holiday` primary key changes from `date` to `(date, state)`; `Station`
  gains a `state` column populated by a one-time backfill.
- **Code**: `src/dbrel/holidays_de.py` (rewritten for per-state lookup), `src/dbrel/models.py`
  (`PublicHoliday`, `Station`), a new `src/dbrel/derive/stop_averages.py`, a new CLI command in
  `src/dbrel/cli.py`.
- **Dependencies**: none added. Point-in-polygon state lookup ended up hand-rolled
  (`geo_states.py`) rather than a new dependency like `shapely` — see design.md for why.
- **Data**: requires the 30-day national backfill (`dbrel backfill`) to already be populated,
  since averages are computed from `trip_stop`, not from the archive directly.
