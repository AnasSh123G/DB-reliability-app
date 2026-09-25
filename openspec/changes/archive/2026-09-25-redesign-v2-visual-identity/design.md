# Design

## Context

See proposal.md (Why). Relevant current state, as inspected:

- **Tokens.** `styles.css` defines the app's own palette (`--ink`, `--panel`, `--line`, `--fog`,
  `--mist`, `--paper`, `--teal`, `--color-amber`, `--color-danger`) plus shadcn's stock token set
  (`--primary`, `--secondary`, `--muted`, `--accent`, `--border`, `--input`, `--ring`,
  `--chart-1..5`, `--sidebar-*`), each with a `:root` (light) and `.dark` (dark) value. Tailwind
  v4's `@theme inline` block maps both sets to utility classes (`bg-panel`, `text-teal`,
  `bg-primary`, …). `.dark` already retints several tokens per mode, not just inverting them
  (e.g. `--teal` is `#165c45` in light, `#15977f` in dark) — v2 scoping its own tokens under a
  `.v2` selector is the same mechanism the codebase already uses for `.dark`.
- **`ShellV2`'s root** already carries a `v2` class (`bahn-buddy-predict/src/components/v2/ShellV2.tsx:25`),
  added by `add-shadcn-interface-v2` "as a marker only. It carries no styles." This change is
  what gives that class actual styles.
- **Delay severity is a shared, unstyled-by-name utility.** `lib/delay.ts`'s `delayColor()`
  returns the literal class names `text-teal` / `text-amber` / `text-danger` for the three delay
  tiers (`<6min` / `6-14min` / `>=15min`). It is reused unchanged by both interfaces. Because
  these are token-backed utility classes, not hardcoded colours, scoping `--teal` /
  `--color-amber` / `--color-danger` under `.v2` retints v2's delay severity without touching
  `delay.ts` or the tier thresholds — the same pattern `.dark` already relies on.
- **Component library.** 45 generated components exist under `src/components/ui/`; v2 currently
  uses only `Card` and `Button`. `cmdk`, `react-day-picker`, `date-fns`, `tailwindcss-animate`
  (`tw-animate-css`, already imported in `styles.css`) and the needed Radix packages are already
  installed — no new dependency is needed for any component this design adopts.
- **Fonts.** `Space Grotesk` (display) and `Inter` (sans) are loaded via a Google Fonts
  `<link>` in `src/routes/__root.tsx`, alongside the `appCss` stylesheet link. Both interfaces
  and the shared root 404/error pages use this same link.
- **Shared root 404/error pages** (`src/routes/__root.tsx`'s `NotFoundComponent` /
  `ErrorComponent`) sit above the route tree, outside `ShellV2` and outside v1's shell. They use
  `bg-background` / `text-foreground` / `bg-primary` / `text-muted-foreground` — today these
  already render with shadcn's untouched stock oklch values for both interfaces, unrelated to
  either `add-shadcn-interface-v2` or this change.

## Goals / Non-Goals

**Goals:**
- v2 has one coherent visual identity — palette, typography, component styling — used
  consistently across all four views, their loading/empty/error/not-found states, desktop and
  phone widths, and light and dark mode, and that identity is distinct from v1's.
- v1's rendering does not change, at all, anywhere.
- No new interactive behaviour: same actions, same triggers, same wording, same API calls as
  today. Only how things look changes.

**Non-Goals:**
- Retiring v1, or carrying this identity to v1.
- Any interaction change that a component swap could smuggle in — e.g. `cmdk`'s keyboard
  selection, `Tabs` hiding one line direction until clicked, `Select`/`Calendar` replacing the
  native inputs. These stay deferred, as in `add-shadcn-interface-v2`'s non-goals, because this
  change is presentation only.
- Reworking the shared root 404/error pages. They are unaffected: not part of the four views the
  spec names, and already rendering with stock/shared tokens today regardless of interface.
- Pixel-diff tooling against v1 — the previous change's verification method is inverted by this
  one; see Decisions (Verification).

## Decisions

### D1. Visual direction: a departure board, not a dashboard
The data v2 already shows — scheduled vs. expected times, `+N min` deltas, platforms,
"Ersatzverkehr (Bus)", "Endet hier" — is literally departure-board data. The identity leans into
that instead of the generic card-grid-with-icons look a fresh shadcn scaffold defaults to:
- A monospaced numeral face for time/delay columns (scheduled time, expected time, `+N min`,
  platform), alongside the existing display/body pairing for prose and labels. This is the
  single biggest visual differentiator from a stock shadcn app, and it's motivated by the data,
  not decoration.
- High-contrast, board-like panels rather than soft-shadow cards; dark mode is treated as the
  primary, most board-like mode, light mode as a genuinely designed second variant (following
  the existing `.dark` convention of retinting, not inverting).
- The existing three-tier delay severity (teal/amber/danger) keeps its semantic meaning and
  thresholds (`delay.ts` is unchanged); only the hues used to express it inside `.v2` may
  change, expressed with more of a signal/indicator treatment than plain coloured text (see D3).
- *Alternative: a generic "modern SaaS" restyle* (soft shadows, gradient accents, pill buttons).
  Rejected — that's a different but equally generic template look, the exact thing the proposal
  moves away from.
- *Alternative: keep v1's ink/panel/teal palette, change only which components render it.*
  Rejected per your answer that colour/type identity is open for reconsideration — keeping v1's
  exact palette would leave the new "distinct from the current interface" requirement barely
  met.
- Exact colour values, type scale and spacing are implementation detail for tasks.md /
  the code, not this document — verified visually (D5), not pinned here.

### D2. Token mechanism: a scoped `.v2` block, reinstating and repurposing `add-shadcn-interface-v2`'s D3
That change removed a `.v2` token block specifically to keep v2 pixel-identical to v1 — the
opposite goal from this change. Reinstate it, now used for its original purpose: `.v2` and
`.v2.dark` blocks in `styles.css` redefine both the app's own tokens (`--ink`, `--panel`,
`--line`, `--fog`, `--mist`, `--paper`, `--teal`) and shadcn's stock tokens consumed anywhere in
v2 (`--primary`, `--secondary`, `--muted`, `--accent`, `--border`, `--input`, `--ring`,
`--chart-1..5`) — the same mechanism `.dark` already uses, scoped one level further. `v1`'s
`:root`/`.dark` values are untouched, so `/v1` keeps rendering exactly as it does today.
- Every shadcn stock token any adopted v2 component resolves (e.g. `Badge`'s `bg-secondary`,
  `Table`'s `border-border`) must have a `.v2` value — a token left unset falls through to
  `:root`'s stock oklch default, which is exactly the "untouched generated default" look this
  change removes. Auditing this is a tasks.md step, not left implicit.
- *Alternative: restyle each component with `className` overrides instead of tokens*, as
  `add-shadcn-interface-v2` did for parity. Rejected — that approach's whole cost was fighting
  every component's own token usage one `className` at a time; scoped tokens are what shadcn
  components are designed to be retinted through.

### D3. Component adoption: only where the result stays behaviourally identical
Extends `add-shadcn-interface-v2`'s D2 table, now judged by "does this change behaviour" instead
of "does this stay pixel-identical":

| v2 piece | Decision |
|---|---|
| View panels | `Card`, restyled to the new identity |
| Train route list, board rows, line runs — desktop | `Table` — tabular data belongs in one; same columns, same data |
| Same, phone width | stays a stacked row list (`Table` doesn't reflow); same responsive split v1 already has, restyled |
| Tags ("Ersatzverkehr (Bus)", "Endet hier", platform, "Teilstrecke", "diese Fahrt", "Nicht überwacht") | `Badge` — same tags, same conditions, styled instead of inline text |
| Notices ("not tracked", "no monitored stops", "pick all fields") | `Alert` — same message, same trigger, no new interaction |
| Loading states | `Skeleton`, shaped to each view's layout, replacing "Lädt…" text — same state, no new behaviour |
| Delay values (expected time, `+N min`) | custom typographic treatment (monospace + severity colour) — no shadcn primitive fits a numeric delta; this is D1's core cue |
| Search input + suggestion dropdown | kept as v1's custom markup, restyled | `cmdk`'s `Command` adds keyboard selection v1 lacks — a behaviour change, still out of scope |
| Direction/stop `<select>`, date/time inputs | native elements, restyled via CSS only | Radix `Select`/`Calendar` behave differently — same reasoning as `add-shadcn-interface-v2` |
| Line view's two directions | both stay shown, no `Tabs` | `Tabs` would hide one direction until clicked — a behaviour change |
| Theme/language toggles | as today, restyled |

The generated `components/ui/*` files are left untouched, as before.

### D4. Typography: add a monospace family, keep the existing pairing
Keep `Space Grotesk` (display) and `Inter` (body) — both are already deliberate, non-default
choices, and D1's differentiator is the numeral treatment, not the prose face. Add one monospace
family for time/delay/platform columns, loaded the same way the existing two are: one more
family on the Google Fonts `<link>` already in `__root.tsx`. No new font-loading mechanism.

### D5. Verification: consistency and distinctness, not pixel parity
`add-shadcn-interface-v2` verified v2 by pixel-diffing it against v1 — the opposite of this
change's goal, so that method is retired for v2's own views. No frontend test runner exists (per
that change's D5), so this change is verified the same way: a manual/Playwright pass against the
spec's own scenarios, using the same temporary-Playwright-install-and-remove approach:
- **Consistency**: screenshots of all four views, their loading/empty/error/not-found states, at
  desktop and phone width, in light and dark mode — reviewed for one coherent identity, and for
  no `.v2` component silently falling back to a `:root` stock token (grep the v2 component
  files for stock-token classes — `bg-primary`, `text-muted-foreground`, `ring-ring`, etc. —
  and confirm each is intentional and has a `.v2` value).
- **Distinctness**: the same four views, same states, compared to v1 — expected to differ.
- **Behaviour unchanged**: the same request-list and behaviour scenarios `add-shadcn-interface-v2`
  used (same API requests in the same order; Prognose gating; outside-click; saved search;
  "no average") re-run against v2 to confirm the redesign changed nothing but appearance.
- **v1 unchanged**: a before/after screenshot diff of v1's four views, confirming this change
  touched nothing under `/v1`.
- `pytest` is not run for this change — nothing outside the frontend's presentation moves.

## Risks / Trade-offs

- **No oracle to verify against.** Parity had a pixel-diff; a new identity only has "is it
  coherent," which is judgment, not a diff. → D5's consistency/distinctness review plus your own
  look at the built pages before this is considered done.
- **Reintroducing `.v2` tokens reintroduces the exact leak `add-shadcn-interface-v2` removed
  them to avoid** — a stock token left un-set anywhere in v2 falls back to `:root`'s generated
  default. → D2's audit step, and D5's grep check, make this explicit rather than incidental.
- **v2 already drifts from v1 in markup** (component swaps), on top of the drift
  `add-shadcn-interface-v2` already accepted (v2's views were generated from v1's source and
  don't track later v1 edits automatically). This change widens that gap further, since v2 no
  longer even shares v1's classes. → Accepted, same reasoning as before: v1 is the frozen legacy
  path until retired, so its own edits (if any) are what still need manually porting to v2 — a
  standing note tasks.md should carry forward.
- **Shared root 404/error pages can't tell v1 from v2.** They're outside both shells. → Left as
  is (Non-Goals); they already look generic today for both interfaces, so nothing regresses.
- **One more font family to load.** → No new mechanism: it rides the existing Google Fonts link
  in `__root.tsx`, already fetched by every page.

## Migration Plan

v2 is already the default at `/`; this change doesn't move any route.
1. Add `.v2` / `.v2.dark` blocks to `styles.css`, initially copied from `:root` / `.dark` (a
   no-visual-change commit that proves the scoping mechanism works before any restyling starts).
2. Restyle `ShellV2`, then `SearchV2`, `TrainV2`, `BoardV2`, `LineV2` onto the new tokens and
   adopted components (D3), one view at a time, each checked against D5's consistency scenario
   before moving to the next.
3. Add the monospace font family to the existing Google Fonts link (D4).
4. Final full pass: D5's consistency, distinctness and behaviour-unchanged checks across all
   four views.

Rollback: revert the `.v2` / `.v2.dark` blocks and the five `v2/*` component files to their
pre-change (pixel-parity-with-v1) versions. v1 is never touched, so rollback is confined to v2.
