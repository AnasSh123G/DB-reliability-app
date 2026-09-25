# Tasks

## 1. Serve the current model artifact

- [x] 1.1 Add a Django view serving `model.onnx` bytes from
      `current_artifact(ML_MODELS_DIR)`, at a URL keyed by the `CURRENT` pointer's stamp (e.g.
      `/api/model/<stamp>/model.onnx`), with a cache-forever header. Verify: fetching it returns
      the same bytes as the file on disk, and a second fetch is served from the browser cache
      (no re-request) without a conditional-GET round trip.
- [x] 1.2 If no artifact exists, the endpoint responds with a clear error, matching the existing
      `NoModelError` message pattern in `predict.py`. Verify: querying it with `ML_MODELS_DIR`
      empty (or `CURRENT` missing) returns that error, not a 500 or empty file.

## 2. Encode (don't infer) a call's model input, server-side

- [x] 2.1 Add a helper next to `_delay_pred` in `web/queries.py` that, given the same
      `Train`/`Station`/scheduled-time context `_delay_pred` already has, builds a
      `ScheduledCall`, runs `compute_features` → `encode_categoricals` → `model_inputs` (reusing
      `dbrel.ml.features`/`dbrel.ml.nn` unchanged), and returns `{cat: number[8], num: number[14]}`
      for a side, or `None` when that side has no scheduled time or resolvable direction. Verify:
      for a sample of real calls, this helper's output equals the intermediate `cat`/`num` arrays
      `predict.py`'s `DelayModel.predict_calls` computes for the same calls, row for row.
- [x] 2.2 Wire this helper into `station_board`, `train_route`, `trips_around` and `line_detail`
      alongside their existing `_delay_pred` calls, adding a `model_input` field (and the current
      artifact's stamp) to each response. Verify: the four endpoints' JSON responses include
      `model_input` for calls that have one, `null` for calls that don't, and the stamp matches
      `CURRENT`.
- [x] 2.3 Confirm no averages table is touched by this path (`stop_delay_average` stays
      `_delay_pred`'s alone). Verify: a query trace or code inspection of the new helper shows no
      read from either averages table.

## 3. Run the model in the browser

- [x] 3.1 Add `onnxruntime-web` to `bahn-buddy-predict`. Load the current model (fetched from the
      task 1 endpoint, using the stamp each API response carries) once, lazily, the first time a
      view needs a prediction; keep the session alive for the rest of the visit. Verify: opening
      any v2 view triggers exactly one model fetch (cached on subsequent views/navigations), and
      a stamp mismatch (a new `CURRENT` while the tab is open) re-fetches rather than mixing
      tensors from two artifacts.
- [x] 3.2 For a view's full set of calls, batch their `cat`/`num` arrays into one `session.run`
      and map the `quantiles` output back to each call's p25/p50/p75 minutes per side. Verify:
      running this against the same calls `dbrel predict-delay` was run against (task 2.1's
      sample) produces matching p25/p50/p75 values, within floating-point tolerance.

## 4. Display both values in v2

- [x] 4.1 Extend `TrainV2`, `BoardV2`, `LineV2` and `SearchV2`'s result panel to show the model's
      prediction next to the existing average for every call, labelled distinctly (e.g. "Ø" /
      "TabM"), following `redesign-v2-visual-identity`'s identity (monospace, severity-coded).
      Verify: a call with both values shows both; a call with only one shows only that one
      (matching the spec's four scenarios); a call with neither keeps today's "no average" state.
- [x] 4.2 Confirm v1 is untouched — no v1 component reads `model_input` or loads
      `onnxruntime-web`. Verify: a diff of v1's component files and route files shows no change
      from before this task group.

## 5. Verification

- [x] 5.1 `npm run build`, `npm run lint` and the backend `pytest` suite pass.
- [x] 5.2 A manual/Playwright pass (temporary install, as in prior changes) across all four v2
      views, both themes and viewports: confirm predictions render, confirm the four
      average/model-availability scenarios each look correct, and confirm v1 is visually
      unchanged.
- [x] 5.3 Confirm the end-to-end story on one real call: the value v2 shows for a chosen call
      matches `dbrel predict-delay`'s output for that same train/stop/date/time.
