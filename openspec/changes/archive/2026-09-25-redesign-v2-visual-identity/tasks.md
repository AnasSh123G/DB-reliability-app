# Tasks

## 1. Token foundation

- [x] 1.1 Add `.v2` and `.v2.dark` blocks to `bahn-buddy-predict/src/styles.css`, initially
      copied from `:root` and `.dark` (app tokens and shadcn stock tokens alike). Verify
      `npm run build` succeeds and v2's rendered pages are pixel-unchanged from before this
      task (the block exists but overrides nothing new yet).
- [x] 1.2 Add the chosen monospace font family to the Google Fonts `<link>` in
      `bahn-buddy-predict/src/routes/__root.tsx`, alongside Space Grotesk and Inter. Verify the
      font loads (network tab / computed style) and is available as a Tailwind font utility.

## 2. Shell and toggles

- [x] 2.1 Restyle `ShellV2` (`bahn-buddy-predict/src/components/v2/ShellV2.tsx`) onto the new
      `.v2` tokens and identity — header, logo mark, theme and language toggles. Verify visually
      at desktop and phone width, light and dark mode, and confirm the toggles still read/write
      `bahnpredict:locale` / `bahnpredict:dark` and stay in sync with v1.

## 3. Search view

- [x] 3.1 Restyle `SearchV2`'s panels and result rows onto the new identity; replace tag text
      with `Badge` and notices with `Alert`. Keep the suggestion dropdown's markup, outside-click
      handling and keyboard behaviour unchanged, restyled only. Verify the same API requests
      fire in the same order for the same action sequence, and Prognose stays disabled until all
      five fields are chosen.

## 4. Train route view

- [x] 4.1 Restyle `TrainV2`'s panel; convert the desktop route grid to `Table`, keep the phone
      stacked list. Apply the monospace/severity-coded treatment to arrival/departure and delay
      values (`delayColor()` stays the source of severity; only the `.v2` hues it resolves to
      may differ). Verify the same columns and data render at both widths and delay values still
      link scheduled time, expected time and `+N min` the same way.

## 5. Station board view

- [x] 5.1 Restyle `BoardV2` to match: `Table` for desktop rows (stacked list on phone),
      `Skeleton` for the loading state, `Badge` for tags, monospace/severity delay values.
      Verify the ±240 min widen action, "not tracked" notice and "nothing scheduled" state still
      behave and trigger exactly as before.

## 6. Line detail view

- [x] 6.1 Restyle `LineV2` onto the new identity, keeping both directions shown (no `Tabs`) and
      the same desktop/phone layout split. Verify both directions still render with the same
      data and neither is hidden behind an interaction.

## 7. Consistency, distinctness and behaviour verification

- [x] 7.1 Grep the five `v2/*` component files for stock shadcn token classes (`bg-primary`,
      `text-muted-foreground`, `ring-ring`, `bg-secondary`, `border-border`, etc.) and confirm
      each one has an intentional `.v2` / `.v2.dark` value — fix any left falling through to the
      `:root` generated default.
- [x] 7.2 Install Playwright temporarily (outside `package.json`, removed after this task group,
      as in `add-shadcn-interface-v2`) and screenshot all four v2 views, including loading,
      empty, error and not-found states, at desktop and phone width, in light and dark mode.
      Review for one coherent identity and no stray stock-token look.
- [x] 7.3 Screenshot the same set for v2 against the equivalent v1 views and confirm they read as
      visually distinct (the spec's "Distinct from the current interface" scenario).
- [x] 7.4 Screenshot all four v1 views, before and after this change, and pixel-diff them.
      Confirm v1 is unchanged — nothing under `/v1` was touched.
- [x] 7.5 Re-run the scripted action sequence (type a query, choose a suggestion, choose
      direction/stop/date/time, press Prognose, open a train/station/line, widen an empty board)
      against v2 and confirm the recorded API requests match those recorded before this change,
      endpoint and parameters, in the same order.
- [x] 7.6 `npm run build` and `npm run lint` pass; remove the temporary Playwright install.
