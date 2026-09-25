# Proposal

## Why

`web-frontend-v2` currently requires the second interface to be pixel-identical to the current
one (`web-frontend`) — same layout, same classes, same stock shadcn tokens left untouched. That
parity was the right goal while v2 was proving itself; now that v2 is the default and v1 is the
frozen legacy fallback under `/v1`, parity is holding v2 back. v2 sits on 45 generated shadcn
components (`accordion`, `alert`, `badge`, `table`, `tabs`, `skeleton`, `calendar`, `select`,
`popover`, `tooltip`, …) and uses only `Card` and `Button`, styled with v1's own classes so it
computes to the exact same pixels. The result reads as a generic shadcn scaffold: uniform
`rounded-2xl` cards, one `size-4` lucide icon per label, and shadcn's stock oklch slate tokens
(`--primary`, `--accent`, `--border`, `--ring`, `--chart-*`) still untouched from their generated
defaults, never pointed at anything the app chose.

## What Changes

- v2 gets its own visual identity, independent of v1's. v1 is unaffected and keeps its current
  look under `/v1`.
- The identity draws on what the app already displays without saying so: live departure-board
  data — scheduled vs. expected times, `+N min` deltas, platforms, "Ersatzverkehr" and "Endet
  hier" tags. v2 is styled to read like a train station departure board (tabular monospaced
  times, signal-coded delay states, high-contrast board panels), not a generic dashboard of
  cards.
- v2 defines its own design tokens, scoped under a `.v2` class (reversing the no-scoped-tokens
  decision from `add-shadcn-interface-v2`, which existed only for pixel parity). v1's `:root`
  and `.dark` tokens are untouched, so `/v1` renders exactly as it does today.
- v2 adopts shadcn components beyond `Card`/`Button` where they fit the board identity:
  `Badge` for tags and delay states, `Table` for the train route and station board, `Tabs` for
  the line view's two directions, `Skeleton` for loading states, `Tooltip` for truncated text.
  Native `<select>`, date and time inputs stay native, as v1 does — this change is presentation,
  not new form controls.
- **BREAKING** (within v2 only): v2 no longer renders pixel-identical to v1. The
  `web-frontend-v2` requirement that ties v2's appearance to v1's is replaced by a requirement
  that v2 has its own coherent visual identity. v2's *behaviour*, wording, and API requests stay
  exactly as they are today — only appearance changes.

## Capabilities

### Modified Capabilities
- `web-frontend-v2`: replaces the "works exactly like the current one" appearance requirement
  (pixel parity, same classes, same tokens) with a requirement that v2 has its own distinct,
  internally consistent visual identity across all four views, states, viewports, and themes.
  The parts of that requirement that are not about appearance — same wording, same control
  behaviour, same actions, same API requests and responses — are kept, unchanged in substance.

## Impact

- **Changed**: `bahn-buddy-predict/src/components/v2/*` (`ShellV2`, `SearchV2`, `TrainV2`,
  `BoardV2`, `LineV2`) — restyled onto the new identity and additional shadcn components.
  `bahn-buddy-predict/src/styles.css` — adds a `.v2` (and `.v2.dark`) token block.
- **Unchanged**: `src/routes/v1/*`, v1's components, `src/lib/api.ts`, `src/hooks/use-dbrel.ts`,
  `src/lib/i18n.ts`, `src/lib/delay.ts`, the generated `src/components/ui/*` files themselves,
  and every API request v2 sends.
- **Dependencies**: none new. The shadcn components v2 adopts are already generated under
  `src/components/ui/`; `tw-animate-css` is already imported in `styles.css`.
- **Verification**: no frontend test runner exists (per `add-shadcn-interface-v2`'s design.md).
  This change is verified by a manual/Playwright visual pass instead of a pixel-diff against v1,
  since parity is exactly what is being removed.
