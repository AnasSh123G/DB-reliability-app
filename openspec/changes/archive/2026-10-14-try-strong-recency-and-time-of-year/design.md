## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Strength of "strong" weighting | Amplitude 2.0: same last-month window, last day 3× | One clear step up from 1.5×. Older data still counts, so the model keeps rare cases |
| Time-of-year encoding | Existing `doy_sin`/`doy_cos` from `features.py` (366-day circle, no year) | Already the project's definition; 31 Dec and 1 Jan sit next to each other |
| How the seasonal inputs reach the model | Computed per row from the prepared `day.bin` and inserted where the with-time-of-year trial `634b5e057ab5fe00` expects them | Re-preparing the corrected data would take hours. That trial has the same vocabulary and the same scaling for every other column (checked) |
| Warm start for the seasonal fine model | `634b5e057ab5fe00/model.pt` (same architecture, built with time of year) | The control fine model warm-starts from its no-time-of-year twin; this keeps the pairing fair |
| Control | Rerun in the new directory | Script edits change the recorded script hash, so the old control runs cannot be reused without hash workarounds |
| Combining both ideas | Not in this run | Testing them separately shows which one, if either, helps |
| Weighting and time of year together | Not tested | Keeps it to 3 conditions (~1.5 h). Can follow if either idea helps |

## Risks

- Time of year likely won't help yet, because no calendar day appears twice in training. The
  report says so plainly instead of treating a null result as a failure.
- 3× weighting puts more emphasis on late July and August. That could hurt if September behaves
  differently. The paired per-day intervals show this either way.
