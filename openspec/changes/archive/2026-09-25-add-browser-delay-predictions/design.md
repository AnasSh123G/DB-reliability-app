# Design

## Context

See proposal.md (Why). What exists today, as inspected:

- **The artifact.** `dbrel.ml.bakeoff.current_artifact(ML_MODELS_DIR)` reads a plain-text
  `CURRENT` pointer file (its content is the artifact's timestamp directory name, e.g.
  `20260924T183944Z` — itself a stable, content-addressed cache key) and resolves it to a
  directory holding `manifest.json` (664 KB: vocab, per-feature training-day mean/std, cards) and
  `model.onnx` (20 MB, ONNX opset, verified bit-for-bit against the PyTorch weights it was
  exported from by `scripts/export_tabm.py`).
- **The ONNX contract.** `dbrel.ml.nn` fixes it: input tensors named `cat` (int, shape
  `(n, 8)`, one column per `manifest["cards"]` entry — station, train, line, category, operator,
  state, weekday, direction) and `num` (float, shape `(n, 14)`, `manifest["numeric"]`'s columns,
  standardised and with sin/cos already expanded for time-of-day). Output tensor `quantiles`,
  shape `(n, 2, 3)` — `SIDES = (dep, arr)` × `(p25, p50, p75)` minutes. `TabMPredictor.predict()`
  is exactly `model_inputs(...)` (build `cat`/`num`) followed by `session.run(...)` (ONNX
  Runtime); nothing else sits between "encoded features" and "the tensors the model reads."
- **Where a call becomes those tensors, today.** `predict.py`'s `DelayModel.predict_calls`
  already does this per scheduled call, offline: `ScheduledCall.raw()` (timetable + holiday
  calendar only, no averages) → `compute_features` → `encode_categoricals` → `model_inputs` →
  inference. Training and prediction share this same `compute_features`/`encode_categoricals`
  code specifically so the two can never drift (`features.py`'s own docstring: "Two separate
  implementations would drift, and a model indexes features by position").
- **Where averages become `arr_pred`/`dep_pred`, today.** `web/queries.py`'s `_delay_pred` is the
  one place that shapes that field, called from `station_board`, `train_route`, `trips_around`
  and `line_detail`. Each call site already has the `Train`/`Station`/scheduled-time context
  `_delay_pred` needs — the same context `ScheduledCall.raw()` needs.
- **The frontend.** v2's five view components (`ShellV2` plus the four views) and `lib/delay.ts`
  already render one `DelayPred`-shaped value per side. `bahn-buddy-predict` has no ONNX/ML
  dependency today.

## Goals / Non-Goals

**Goals:**
- The browser runs the model itself, via `onnxruntime-web` — not a second Django-side inference
  path. That was the explicit, prior decision for where TabM should run.
- Zero duplicated feature/vocabulary/normalisation logic in TypeScript. The browser receives
  already-encoded tensors and only runs them through the model.
- Every call that already shows a stored average also shows the model's prediction, in every v2
  view, the moment inference finishes — degrading to "average only" if the model hasn't loaded or
  has nothing to predict.

**Non-Goals:**
- Any change to v1, to the averages pipeline, or to `delay-model`'s training/evaluation/CLI paths.
- A second Django-side prediction endpoint that runs the model server-side. `DelayModel.predict_calls`
  stays CLI-only; this change adds a sibling that stops one step earlier (encode, don't infer).
- Model management UI, A/B comparison, or accuracy telemetry from the browser. This change is
  "show what the model says next to what the averages say," nothing more.
- Combining the two into one number. They stay two values; a later change can revisit that once
  there's real signal on how they compare in the wild.

## Decisions

### D1. The web layer encodes; the browser infers
`web/queries.py` gains a helper — call it alongside `_delay_pred`, same call sites — that builds
a `ScheduledCall`, runs it through `compute_features` → `encode_categoricals` → `model_inputs`
(the exact functions `predict.py` already uses and the export script already double-checked
against training), and returns `{cat: number[8], num: number[14]}` per side. It stops before
`session.run`. The browser's `onnxruntime-web` session takes those arrays as its `cat`/`num`
inputs unchanged.
- *Alternative: encode in TypeScript from raw fields (station name, category, coordinates, …).*
  Rejected. It would reimplement vocabulary lookup, standardisation and cyclic time encoding a
  second time, in a second language, exactly the drift `features.py` was written to prevent. A
  station or category added after the model's last export would need its `UNKNOWN` fallback
  reimplemented correctly in TS too, with no test tying it to training's behaviour.
- *Alternative: run inference in Django and return `arr_pred_tabm`/`dep_pred_tabm` directly.*
  Rejected — this is exactly the "second server-side prediction path" the project already
  decided against; the model was sized and exported for the browser specifically so it would
  *not* need to run on the server per request.

### D2. The model is served content-addressed, by its artifact stamp
A new endpoint serves `model.onnx` at a URL keyed by the `CURRENT` pointer's stamp (e.g.
`/api/model/<stamp>/model.onnx`), set to cache forever — the stamp only changes when a new
artifact is exported, and an artifact's bytes never change once written (`bakeoff.py`'s own
"a second run never touches the [previous] artifact" invariant). Each API response that includes
a `model_input` also includes the current stamp, so the frontend knows which model those tensors
belong to and when to re-fetch. `manifest.json` (664 KB of vocab/stats) is never sent to the
browser — encoding already happened server-side, so the browser needs the weights, not the
vocabulary.
- *Alternative: version by content hash instead of the existing timestamp stamp.* Rejected as
  unnecessary — the stamp is already unique and immutable per artifact; hashing 20 MB per request
  to name a URL buys nothing the existing pointer doesn't.

### D3. `onnxruntime-web`, WASM backend, loaded once and reused
v2 loads the model once (module-level, like `SearchV2`'s `savedSearch`) the first time any view
needs a prediction, and reuses the session across views and navigations within the visit — a
fresh page load re-fetches (the model is cached by the browser per D2, so this is normally a
cache hit, not a re-download). Inference runs on a batch of calls at once per view (a station
board's whole call list in one `session.run`, not one per row), matching how `TabMPredictor`
already batches.
- *Alternative: WebGPU backend.* Not pursued now — WASM needs no capability detection or
  fallback path, and the model is small enough (20 MB, batches of tens of rows) that WASM
  inference latency is not expected to be the bottleneck. Revisit only if it measurably is.

### D4. Display: a labelled second value, not a merge
Each call's existing average display gains a second, equally-placed value for the model's
p50 (and, if the identity's departure-board treatment wants it, the p25–p75 band as a range).
Both are labelled distinctly (e.g. "Ø" for the average, "TabM" for the model) so neither reads as
more authoritative than the other. Exact placement/labelling is a `redesign-v2-visual-identity`-
style visual decision made during implementation, verified visually — not pinned here.

## Risks / Trade-offs

- **20 MB download on first visit.** → Content-addressed, cache-forever (D2); WASM decode/init
  cost is paid once per browser, not per view.
- **`model_input` grows every response that lists calls** (a station board can list dozens).
  Each side's `cat`+`num` is 22 numbers — small per call, but it adds up on a busy board. →
  Acceptable: still far smaller than shipping `manifest.json`'s 664 KB vocab once, and these are
  the same responses already paying for `arr_pred`/`dep_pred`.
- **A call outside the model's training vocabulary** (a new station or train). → Already
  handled: `encode_categoricals` maps it to `UNKNOWN`, exactly as offline prediction does; no new
  behaviour needed here, just confirmed by the spec's added scenario.
- **Model updates**: a bake-off run moving `CURRENT` while a browser tab is open with the old
  model cached. → Harmless: `model_input` and the stamp travel together in each response: the
  browser either matches its cached model to the stamp it received, or re-fetches. No stale
  cross-artifact mixing is possible because inference never mixes tensors from two stamps.
- **This duplicates "server always knows the truth" for encoding** — if `compute_features`
  changes shape without a re-export, the server would silently send tensors the deployed
  `model.onnx` doesn't expect. → Not new: `predict.py`'s CLI path has this exact same coupling
  today, and the export script already guards it (checks 1–3 in `export_tabm.py`). This change
  adds no new failure mode, only a new consumer of the same guarantee.

## Migration Plan

Purely additive; no existing behaviour changes if `onnxruntime-web` fails to load or a view never
calls it — the current "average only" display is exactly what still shows.
1. Add the model-serving endpoint and the `model_input` encoding helper; verify its output against
   `predict.py`'s intermediate arrays on a sample of real calls (same technique
   `export_tabm.py` already uses to verify ONNX against PyTorch).
2. Add `onnxruntime-web` to `bahn-buddy-predict`; load the model and run one view's calls through
   it; verify the browser's output matches the CLI's `predict-delay` output for the same calls.
3. Wire the display into all four v2 views.
4. Verify visually and behaviourally (as `redesign-v2-visual-identity`'s D5 did): all four views,
   both themes, both viewports, plus the specific edge cases in the spec (average only, model
   only, neither, both).

Rollback: remove the endpoint, the helper, and the frontend's `onnxruntime-web` usage; v2 reverts
to showing averages alone, which is exactly today's behaviour.
