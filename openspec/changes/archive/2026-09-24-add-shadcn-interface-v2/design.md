# Design

## Context

See proposal.md (Why). What the current code looks like, as inspected:

- **Views.** Five hand-built components, 943 lines in total:
  - `AppShell.tsx` (147) holds the theme and language toggles, header and `useAppChrome`, which
    keeps locale and dark mode in `localStorage` under `bahnpredict:locale` / `bahnpredict:dark`.
  - `TrainSearch.tsx` (361) is the search view.
  - `TrainOverview.tsx` (208) is the train route view.
  - `StationBoard.tsx` (123) and `LineDetail.tsx` (104) are the board and line views.
- **Routes.** `src/routes/{index, train/$trainId, station/$stationId, line/$lineId}.tsx` are
  file routes. Each wraps its view in `AppShell`.
- **Data layer.** It is separate and reused unchanged: `lib/api.ts`, `hooks/use-dbrel.ts`,
  `lib/i18n.ts` (the `copy` dictionary, de/en), and `lib/delay.ts` (`delayColor`,
  `expectedTime`).
- **Theme.** `styles.css` defines the app palette as CSS variables (`--ink`, `--panel`,
  `--line`, `--fog`, `--mist`, `--paper`, `--teal`, `--action-foreground`, plus `--amber`) in
  `:root` (light) and `.dark`.
  - `--background` and `--foreground` are already mapped to `--ink` and `--paper`.
  - shadcn's other tokens (`--card`, `--popover`, `--primary`, `--muted`, `--accent`,
    `--border`, `--input`, `--ring`) are still stock slate values.
  - The current interface's root "not found" and error pages use `bg-primary` / `text-muted-foreground`.
- **Tooling.** The only scripts are `build` and `lint`; there is no frontend test runner.
  Django's `frontend_proxy` forwards every non-`/api` path to the node server on :8901.

## Goals / Non-Goals

**Goals:**
- A v2 interface, built from `src/components/ui/*`, that works exactly like v1: the same
  pixels, wording, behaviour, and API requests and responses. It is the default at `/`.
- No change to v1's rendered output or behaviour, other than its addresses moving under `/v1`.

**Non-Goals:**
- Retiring v1.
- Anything v2 would do differently from v1, including improvements such as keyboard navigation
  of the suggestion list or a calendar date picker. They belong in a later change that applies
  them to both interfaces, or to v2 once v1 is retired.
- Changing API endpoints, data fetching, or wording. A missing i18n string for a new
  control, such as a date-picker placeholder, is added to `copy` without changing existing
  strings.
- Showing model predictions (that belongs to change `add-offline-delay-model` and its browser
  follow-up).
- New views or features beyond v1's.

## Decisions

### D1. Same app; v2 at the root, v1 under `/v1`
v2's file routes are `src/routes/index.tsx`, `train/$trainId.tsx`, `station/$stationId.tsx` and
`line/$lineId.tsx`. Its components live in `src/components/v2/` (`ShellV2`, `SearchV2`,
`TrainV2`, `BoardV2`, `LineV2`).
- v1's routes move unchanged to `src/routes/v1/` (`/v1/`, `/v1/train/$trainId`, …). Its
  components change only their link targets, which now point under `/v1`: the logo, the back
  button, and the search-result, train, line and station links.
- A shared `v2Paths` helper builds every in-app link in v2, e.g.
  `{ to: "/train/$trainId", params }`, so no v2 component can link into v1 by accident.
- `src/routes/v2/index.tsx` and a `src/routes/v2/$.tsx` catch-all redirect the old `/v2`
  addresses to the root.
- *Alternative considered: a separate Vite app.* Rejected. It would duplicate the data layer
  and need a second server process or port behind the proxy.

### D2. Component mapping: a shadcn component only where it is indistinguishable
The rule: a v1 hand-built piece is replaced by a shadcn component only if the result renders
pixel-identically and behaves identically, including keyboard, focus, hover and pointer
behaviour. Otherwise v2 keeps v1's markup for that piece. `className` overrides are used to
match v1's look. The generated `components/ui/*` files are never edited.

| v1 piece | v2 |
|---|---|
| Result panel / view panels (`rounded-2xl` sections) | `Card`, styled to v1's classes |
| Buttons (Prognose, suggestion rows, result rows, back, toggles) | `Button`, as v1 already does |
| Search input + suggestion dropdown with its outside-click handler | v1's markup and handler. cmdk adds arrow/Enter selection and highlight, which v1 lacks. |
| Direction and stop `<select>`, date and time inputs | the native elements, as v1. Radix `Select` and `Calendar` look and behave differently. |
| Tags ("Ersatzverkehr (Bus)", "Endet hier", platform, "Teilstrecke", "diese Fahrt", "Nicht überwacht") | plain inline text as v1. An outlined `Badge` changes the look and the wrapping. |
| Notices ("not tracked", "no monitored stops", "pick all fields") | v1's markup. An `Alert` may be used only if it renders identically. |
| Train route list (desktop grid + mobile stack), board rows, line runs | v1's layout. A `Table` may be used only if it renders identically. The line view shows both directions stacked on phones, as v1, with no `Tabs`. |
| Loading states | v1's "Lädt…" text, with no `Skeleton` |
| Theme and language toggles | as v1 |

`savedSearch` (module-level, survives in-app navigation, resets on reload) is copied into
`SearchV2` as its own module variable. v1 and v2 searches are independent.

### D3. Theme: v1's classes under v1's tokens, with no scoped token block
v2 renders v1's own classes (`bg-panel`, `border-line`, `text-paper`, …), so it needs no theme
of its own. Early in this change a `.v2` block in `styles.css` pointed shadcn's tokens
(`--primary`, `--ring`, `--border`, …) at the app palette. It was removed because it made v2
differ from v1 wherever v1's markup still falls back to a stock token. The keyboard focus ring
on every `Button` is one example: it is slate in v1 and would turn teal in v2. With no token
block:
- v2 and v1 compute the same colours for the same markup.
- v1 and its root 404/error pages keep today's values. The 404 and error pages' "Go home" link
  now leads to v2, the default.
- `ShellV2` sets no classes on `document.documentElement`. v2 uses no portalled menus, so none
  are needed.
- Its root keeps a `v2` class, as a marker only. It carries no styles.

`Card` panels take v1's panel classes. `text-paper` and `shadow-none` cancel `Card`'s own
`text-card-foreground` and `shadow`, so a `Card` panel computes exactly like v1's `<section>`.
The generated `components/ui/*` files are left untouched.

### D4. Locale and theme state shared with v1
`ShellV2` reuses `useAppChrome()` from `AppShell.tsx`, so both interfaces read and write the same
`localStorage` keys. That covers the spec's shared language and theme choices.

### D5. Verification
There is no frontend test runner, and adding Vitest or a similar runner is out of scope. Each
task is verified by `npm run build` and `npm run lint`, then a browser check with Playwright
driving the built node server. As in previous changes, Playwright is installed temporarily
outside `package.json` and removed afterwards. API responses are recorded once and replayed, so
the live data cannot make two captures differ. The browser check covers:
- **pixels:** screenshots of v2 and v1, pixel-diffed:
  - the four views, at desktop and phone width, in light and dark mode
  - search states: empty, suggestions open, form filled, results shown
  - loading, empty-board and not-found states
  Every pair must be identical.
- **requests:** the list of API requests (endpoint and parameters, in order) recorded for the
  same scripted action sequence in both interfaces. The two lists must be identical.
- **behaviour:** every spec scenario. This includes the gate, outside click, round trip, link
  targets under `/` and `/v1`, the `/v2` redirects, and shared theme and language.
- **v1 unchanged:** a before/after screenshot diff of the four v1 views, now under `/v1`.

The backend test suite (`pytest`) is run once at the end, to show nothing outside the frontend
moved.

## Risks / Trade-offs

- **Exact parity leaves little room for shadcn components.** Many would change a pixel or a
  behaviour, so v2 keeps v1's markup for those pieces (D2). → Accepted: working exactly like
  v1 comes first. The remaining gains are the shared `Card`/`Button` primitives and a separate
  v2 codebase that later changes can move to shadcn components one at a time, each proven
  identical before it lands.
- **v2 drifts from v1 when v1 changes.** v2's views were generated from v1's source files, so
  a later edit to v1 is not carried over automatically. → Until v1 is retired, any change to
  a v1 view is applied to its v2 copy too (see the standing note in tasks.md).
- **Two interfaces to keep in sync until one is retired.** → Both share the whole data layer,
  so only presentation is duplicated. Any behaviour fix must be applied to both until then. The
  tasks note this for future changes.
- **Old bookmarks.** v1 addresses bookmarked before the switch (`/train/<id>`) now open v2. →
  Harmless: v2 shows the same thing.
- **Inherited v1 bug.** On a full page load with the German locale, a stored light theme is
  shown dark. The server renders dark, and React does not correct the mismatched `class`
  attribute on hydration. v2 reuses v1's `useAppChrome`, so it behaves the same way, as exact
  parity requires. Fixing it is a separate change covering both interfaces.

## Migration Plan

v2 becomes the default at `/`, and v1 moves to `/v1`. `/v2` addresses redirect to the root.
To roll back:
1. Move `src/routes/v1/*` back to the root.
2. Revert v1's link targets.
3. Delete `src/routes/v2/`, `src/components/v2/` and the `.v2` block in `styles.css`.
4. Rebuild.
