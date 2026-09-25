# Tasks

Standing notes:
- First check whether each task needs the current model. Delegate pure execution (builds,
  server restarts, the Playwright run) to the `task-runner` subagent, and a spec-scenario
  re-check to `verify-claim`.
- Until v1 or v2 is retired, any behaviour fix in one interface must be mirrored in the other.
- v2 must work exactly like v1 (spec: "works exactly like the current one"). Where a 3.x task
  names a component that changes a pixel or a behaviour (`Popover`+`Command`, `Select`,
  `Calendar`, `Badge`, `Tabs`, `Skeleton`, a `Table` that renders differently), section 6
  replaces it (design D2).
- No frontend test runner is added (design D5). "Build + lint" means `npm run build && npm run lint`
  in `bahn-buddy-predict/`, both passing with no new warnings.

## 1. Baseline and theme

- [x] 1.1 Capture baseline screenshots of the four v1 views (`/`, `/train/<id>`,
      `/station/<id>`, `/line/<id>`), in light and dark mode, with Playwright against the
      running server. Verify: 8 PNGs saved under the job scratch directory, for the
      final-diff task.
- [x] 1.2 Add the `.v2` / `.dark.v2` token block to `src/styles.css` (design D3). Verify: build +
      lint pass. A v1 screenshot re-capture is pixel-identical to 1.1.

## 2. Shell and routing

- [x] 2.1 Add `src/components/v2/paths.ts` (the `v2Paths` link helpers) and `ShellV2`: the header,
      `ToggleGroup` theme/language toggles and `useAppChrome` reuse, plus the `v2` / `dark`
      classes on `documentElement` while mounted. Verify: build + lint pass. `/v2` renders the
      shell in both themes, and the language and theme choices carry over to `/` and back.
- [x] 2.2 Add the four file routes under `src/routes/v2/`, rendering placeholder cards inside
      `ShellV2`. Verify: `routeTree.gen.ts` regenerates, build passes, and each route loads
      through Django on :8000.

## 3. Views

- [x] 3.1 `SearchV2`:
      - a `Popover` + `Command` combobox with `shouldFilter={false}`
      - `Select` for direction and stop, a `Calendar` date picker with a de/en locale, and a time `Input`
      - the Prognose `Button` and a result `Card`
      - `savedSearch` copied over, and every link built through `v2Paths`
      Verify with Playwright:
      - arrow/Enter chooses a suggestion and Escape closes the list
      - an outside click closes the list
      - Prognose stays disabled until all five inputs are set
      - the request carries `YYYY-MM-DD`
      - results open `/v2/train/<id>`, and going back restores the search
- [x] 3.2 `TrainV2`:
      - a `Card` header with a `Badge` for a replacement bus
      - a `Table` on ≥sm and a stacked list on mobile
      - `Badge` for "not monitored" and `Alert` for "no monitored stops"
      - line and station links under `/v2`
      Verify: same stops, times and delay figures as `/train/<id>` for the same id, and the links
      stay in `/v2`.
- [x] 3.3 `BoardV2`: a `Card` and `Table` of calls, with `Badge`s for "ends here" and platform,
      an `Alert` for an untracked station, and a `Button variant="link"` for ±240 min. Verify:
      same calls as `/station/<id>`, the untracked-station alert appears for an untracked id, and
      train links stay in `/v2`.
- [x] 3.4 `LineV2`: a `Card` per direction side by side on ≥sm and `Tabs` below sm, with
      `Badge`s for "part of route", "this run" and "replacement bus". Verify: same runs and
      delays as `/line/<id>`, the tabs switch directions on a narrow viewport, and train links
      stay in `/v2`.
- [x] 3.5 Loading and not-found states in all four views, using `Skeleton` rows and the same
      texts as v1. Verify: throttled-network screenshots show skeletons, and unknown ids show
      v1's messages.

## 4. Verification

- [x] 4.1 Run every spec scenario in `specs/web-frontend-v2/spec.md` with Playwright against
      the rebuilt server [task-runner to run it; verify-claim to re-check the result].
      Verify: every scenario passes, as a list of scenario to pass/fail.
- [x] 4.2 Diff the v1 screenshots against the 1.1 baseline, after a v2 → v1 client-side
      navigation too. Verify: all 8 are pixel-identical.
- [x] 4.3 Run the backend `pytest` suite, uninstall the temporary Playwright, rebuild, and
      restart the server. Verify: all tests pass, `package.json` and `bun.lock` are unchanged
      apart from the intended files, and the stray `package-lock.json` npm creates is removed.

## 5. Default interface

- [x] 5.1 Move the v2 routes to the root (`/`, `/train/$trainId`, `/station/$stationId`,
      `/line/$lineId`) and v1's routes to `src/routes/v1/`. Point `v2Paths` at the root
      paths. Verify: `routeTree.gen.ts` regenerates and every address returns 200 through
      Django on :8000.
- [x] 5.2 Point v1's link targets under `/v1`: the logo, the back button, and the
      search-result, train, line and station links. Verify: every link on the four `/v1`
      views, and a v1 search result, stays under `/v1`.
- [x] 5.3 Redirect `/v2` and `/v2/<rest>` to `/` and `/<rest>`. Verify: `/v2` and
      `/v2/train/2259` return 307 to `/` and `/train/2259`.
- [x] 5.4 Verify: build, typecheck and lint pass (lint unchanged at the 63 existing problems),
      and 21/21 browser checks pass. They cover which interface each address shows, the link
      targets in both interfaces, the redirects, v2 → v1 client-side theme cleanup, and the 404
      page.

## 6. Exact parity with v1

- [x] 6.1 Search view: v1's suggestion input and dropdown markup, including the outside-click
      handler and no arrow/Enter selection. Native direction and stop `<select>`, native date
      and time inputs. v1's "pick all fields" hint and result rows, with result rows as buttons
      that navigate. Verify: v1/v2 screenshot pairs identical for empty, suggestions open,
      form filled and results shown.
- [x] 6.2 Train, board and line views: v1's layout for the route list, board rows and line
      runs. Tags as plain inline text, notices in v1's markup, both line directions stacked on
      phones (no `Tabs`). Verify: v1/v2 screenshot pairs identical for the four views at
      desktop and phone width, in light and dark mode.
- [x] 6.3 Loading, empty-board and not-found states as v1 ("Lädt…" text, no `Skeleton`).
      Verify: v1/v2 screenshot pairs identical, with responses delayed for the loading state.
- [x] 6.4 Request parity: record the API requests (endpoint and parameters, in order) for the
      same scripted action sequence in `/v1` and `/`. Verify: the two lists are identical.
- [x] 6.5 Re-run every spec scenario and the v1 before/after diff (design D5), then run
      `pytest`, remove the temporary Playwright, rebuild and restart the server [task-runner to
      run it; verify-claim to re-check the result]. Verify: every scenario passes, every
      screenshot pair is identical, and the request lists match.
