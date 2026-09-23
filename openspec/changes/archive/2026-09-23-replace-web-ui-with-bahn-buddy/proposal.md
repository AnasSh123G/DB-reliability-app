# Proposal

## Why

The current `dbrel` web UI (Django-rendered `search.html`/`station.html`/`train.html`/`line.html`)
is a functional but plain browse tool. A richer frontend template, `bahn-buddy-predict`
(a React/TanStack Start app generated with Lovable), already exists in the project and shows
delay information — per-stop scheduled vs. expected arrival/departure, platform, direction — in
a more detailed, better-designed way than today's UI. The user wants to adopt it as the
project's frontend outright, keeping all of the extra detail it displays, with every figure
backed by real data from the `dbrel` database instead of the template's current hardcoded mock
data.

## What Changes

- Adopt `bahn-buddy-predict` as the sole `dbrel` web frontend, replacing the Django-rendered
  pages. **BREAKING**: `search.html`, `station.html`, `train.html`, `line.html` and their
  `views.index`/`station`/`train`/`line` handlers are removed; the URL structure changes from
  server-rendered pages to a client-rendered SPA.
- Wire the frontend's train-search and train-route views to the existing `dbrel` JSON API
  (`/api/suggest`, `/api/train/<id>/route`, `/api/line/<id>/around`, etc.) via
  `@tanstack/react-query`, which is already provisioned in the template but currently unused.
- Delete every piece of mock/synthetic data and logic from the template: `TRAIN_SERVICES`,
  `getStopDelay()`'s deterministic fake per-stop delay cycle, `getTrainDirections()`'s
  fabricated reverse-direction leg, and `TrainSearch`'s synthetic "3 runs 120 minutes apart"
  headway. No synthetic data may remain reachable from the shipped app.
- Replace the template's fake "expected/predicted" arrival and departure fields with the real
  precomputed average from `stop_delay_average`, surfaced through the `dep_pred`/`pred` fields
  in `queries.py` (currently hardcoded `None` in every response). These are historical typical
  delays, not a live per-trip forecast — `dbrel` has no live prediction model at present, and
  this change does not add one.
- Add two new views to the frontend, designed in the template's own visual language (shadcn/ui
  components, `ink`/`panel`/`teal` palette, DE/EN + light/dark toggles), with no template
  precedent to draw from today: a **station board** (replacing `station.html`) and a **line
  detail** view (replacing `line.html`), so the Django UI can be retired in full rather than
  partially.
- Extend the JSON API (`queries.py`/`views.py`) as needed to carry every field the frontend
  displays that isn't already returned — e.g. `platform_code`, `origin`/`destination` names,
  `n_stops`/`n_stops_tracked`, cancellation/quality state — rather than trimming the frontend's
  displayed detail down to what the API already happens to return today.
- Decide and implement how the built frontend is served alongside the existing Django process
  in this (self-hosted, non-Cloudflare) deployment, since the template's default build target
  (`nitro` → Cloudflare Worker) does not match how `dbrel` is actually deployed.

## Capabilities

### New Capabilities
- `web-frontend`: the React/TanStack Start frontend's pages, the real data each one displays,
  and the API contract it relies on — search, train route, station board, and line detail, all
  sourced from live `dbrel` data with zero mock/synthetic content.

### Modified Capabilities
(none — `archive-backfill`, `holiday-calendar`, and `stop-delay-averages` are consumed as-is;
no delta to their requirements)

## Impact

- **Removed**: `src/dbrel/web/templates/dbrel/{search,station,train,line,_directions}.html`,
  `src/dbrel/web/static/dbrel/{app.css,app.js,browse.js}`, and the Django view functions that
  render them (`views.index`, `views.station`, `views.train`, `views.line`).
- **Kept and extended**: `src/dbrel/web/queries.py` and the JSON endpoints in `views.py`/
  `urls.py` — these become the sole surface the frontend talks to, and gain the fields/endpoints
  the frontend needs that don't exist yet (e.g. a JSON station-board endpoint already exists;
  a line-detail JSON shape may need filling out to match `line_detail()`'s server-rendered
  version).
- **Replaced wholesale**: `bahn-buddy-predict/src/lib/trains.ts` (deleted, mock data), `src/lib/
  prediction.ts` (deleted or repurposed), `TrainSearch.tsx`/`TrainOverview.tsx` (rewritten
  against real API data), plus two new components/routes for station board and line detail.
- **New**: a served/deployed home for the built `bahn-buddy-predict` app in the existing
  self-hosted `dbrel` deployment (not Cloudflare/nitro as scaffolded).
- **Dependency surface**: this change relies on `stop_delay_average` (from the already-archived
  `add-holiday-aware-stop-averages` change) as the sole source of "expected" delay figures.
