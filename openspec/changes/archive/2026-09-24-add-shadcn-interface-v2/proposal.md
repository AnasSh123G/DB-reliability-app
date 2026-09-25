# Proposal

## Why

The `bahn-buddy-predict` frontend already has shadcn/ui set up: `components.json` exists and 45
components are generated under `src/components/ui/`. Yet the app uses only `Button`.
Everything else is hand-built Tailwind markup:
- the search suggestion dropdown, including its own outside-click handling
- native `<select>`, date and time inputs
- the route, board and line lists, laid out with hand-tuned CSS grids
- cards, notices, and the theme and language toggles

That markup is harder to keep consistent and maintainable than the library it already ships.
A second interface built on shadcn components, working exactly like the current one, can replace
it as the default while the current one stays reachable until it is retired.

## What Changes

- Add a **second interface and make it the default**, in the same TanStack Start app. It has
  the same four views as the current one:
  - search: `/`
  - train route: `/train/$trainId`
  - station board: `/station/$stationId`
  - line detail: `/line/$lineId`
- v2 **works exactly like the current interface**: the same layout and look down to the pixel,
  the same wording, the same behaviour, and the same API requests and responses. It adds no
  behaviour the current interface lacks and drops none it has.
- Build v2 from shadcn components wherever a component renders and behaves exactly like the
  current markup. Where none does, for example the browser's native select, date and time
  inputs, v2 keeps the current markup.
- v2 uses v1's own palette classes, so it needs no theme of its own. shadcn's stock tokens are
  left as they are, exactly as v1 sees them.
- Reuse the existing data layer unchanged: the API client, the data hooks, the German/English
  wording and the delay formatting.
- The **current interface moves under `/v1`** (`/v1`, `/v1/train/…`, `/v1/station/…`,
  `/v1/line/…`). Its in-app links point under `/v1`. Its look, wording and behaviour are
  otherwise unchanged. Retiring it is a later decision.
- The `/v2/…` addresses, where v2 was served before it became the default, redirect to the
  same view at the root.

## Capabilities

### New Capabilities
- `web-frontend-v2`: the second, shadcn-based interface. It is the default at the root, works
  exactly like the current interface, stays within itself when navigating, and moves the
  current interface under `/v1`. v2's views are generated from v1's view files.

### Modified Capabilities
(none. `web-frontend` requirements name no addresses, so they keep applying to the current
interface under `/v1`, unchanged. v2 meets them by working exactly like it.)

## Impact

- **New code**:
  - `bahn-buddy-predict/src/components/v2/` holds the v2 views and shell.
  - `bahn-buddy-predict/src/routes/{index, train, station, line}` now hold the v2 file routes.
  - `bahn-buddy-predict/src/routes/v1/` holds the current interface's routes, moved from the
    root.
  - `bahn-buddy-predict/src/routes/v2/` holds only the redirects from the old `/v2` addresses.
  - `routeTree.gen.ts` regenerates.
- **Changed in the current interface**: only its link targets, in `AppShell.tsx`,
  `TrainSearch.tsx`, `TrainOverview.tsx`, `StationBoard.tsx` and `LineDetail.tsx`, which now
  point under `/v1`.
- **Reused as is**: `src/lib/api.ts`, `src/hooks/use-dbrel.ts`, `src/lib/i18n.ts`,
  `src/lib/delay.ts` and the existing `src/components/ui/*`.
- **Dependencies**: none new. `cmdk`, `react-day-picker`, `date-fns` and the Radix packages the
  components need are already installed.
- **Backend**: none. Django's catch-all already forwards every non-`/api` path to the frontend
  server, so `/v1/...` and the `/v2/...` redirects are served with no backend change.
- **Bookmarks**: a bookmarked current-interface address such as `/train/<id>` now opens the
  same view in v2. It shows the same data, because v2 works exactly like the current
  interface.
- **Deployment**: the frontend is rebuilt (`npm run build`) and the node server restarted, as
  for any frontend change.
