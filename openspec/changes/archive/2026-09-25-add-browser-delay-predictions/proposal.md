# Proposal

## Why

`delay-model` trains, evaluates and exports a TabM neural net (`data/models/v<N>/<stamp>/model.onnx`,
verified byte-for-byte against PyTorch) and can predict from the CLI, but nothing outside that CLI
can reach it. The frontend (v2) shows only the stored historical average for each call. The model
was deliberately sized and shaped for the browser (ONNX export, ≤35 MB), per the project's own
prior decision that TabM inference belongs client-side, in `onnxruntime-web` — not as a second
Django-side prediction service. This change is what actually gets it there, so v2 can show the
average and the model's prediction side by side.

## What Changes

- The backend adds two small server-side pieces, reusing `dbrel.ml.features`/`predict.py`
  unchanged (the tested, training-verified encoding — never reimplemented in TypeScript):
  - An endpoint serving the current model's `model.onnx` bytes (from `ML_MODELS_DIR`/`CURRENT`),
    cacheable by content hash so the browser fetches it once and reuses it across pages.
  - For every call already returned by `station_board`, `train_route`, `trips_around` and
    `line_detail`, a compact, already-encoded `model_input: {cat: number[8], num: number[14]}`
    per side needing a prediction — the exact tensors `model.onnx` expects, computed the same way
    `predict.py` does. No feature engineering, vocabulary or normalization logic is duplicated in
    the frontend.
- v2 loads `onnxruntime-web` and the model once, runs inference client-side on the `model_input`
  each view already received, and shows the result (p25/p50/p75 minutes) next to the existing
  average for every call, in every view. Values that can't be modeled (no scheduled time, no
  resolvable direction) show no model prediction, matching how a missing average is shown today.
- **v1 is not touched.** Per the established split (`add-shadcn-interface-v2`,
  `redesign-v2-visual-identity`), v1 stays the frozen legacy interface under `/v1`; this lands in
  v2 only. (Flagging this assumption — say if both should get it.)
- **BREAKING**: none. Existing API fields are unchanged; `model_input` is additive. A browser
  without model inputs (or before the JS loads) still shows the average alone, as today.

## Capabilities

### New Capabilities
(none — this is new behaviour for two existing capabilities, not a new one)

### Modified Capabilities
- `delay-model`: adds a requirement that the current model artifact and its per-call encoded
  inputs are servable over HTTP, using the same feature/vocabulary code prediction already uses.
- `web-frontend-v2`: adds a requirement that every call showing a stored average also shows the
  model's prediction, computed client-side, wherever both are available.

## Impact

- **New backend code**: a view serving `model.onnx` (streamed from `ML_MODELS_DIR`/`CURRENT`,
  content-hash cacheable); a helper alongside `_delay_pred` in `web/queries.py` that builds
  `model_input` for a call via `dbrel.ml.features`/`predict.py`'s `ScheduledCall`/
  `compute_features`/`encode_categoricals` (not `DelayModel.predict_calls`, which also runs
  inference — the web layer only encodes; the browser infers).
- **New frontend dependency**: `onnxruntime-web` (WASM backend; no server-side ONNX runtime
  involved in serving these views).
- **Changed**: `station_board`, `train_route`, `trips_around`, `line_detail` query functions gain
  a `model_input` field per prediction; `DelayPred`-consuming v2 components
  (`TrainV2`/`BoardV2`/`LineV2`/`SearchV2`) render a second value per call.
- **Unchanged**: `stop_delay_average` and the averages pipeline; `delay-model`'s training,
  evaluation and CLI serving; v1 entirely; the ONNX model file itself and its manifest.
- **Ops**: model updates already move `CURRENT` atomically (existing bake-off/export behaviour);
  the new HTTP endpoint just needs to reflect that pointer, including its cache key.
