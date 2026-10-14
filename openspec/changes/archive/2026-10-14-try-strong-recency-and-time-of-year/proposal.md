## Why

**Overall goal (unchanged):** make the app's delay forecasts more accurate.

[finish-recency-comparison](../finish-recency-comparison) found that mild recency weighting made
no practical difference: the last month counted up to 1.5×, and the results moved by about 0.1
percentage point. Two ideas remain untested on the corrected labels:

1. **Stronger recency weighting.** Maybe 1.5× was too gentle to matter.
2. **Time of year as an input.** Give the model the service date's day and month, with no year,
   as a point on a circle. This lets it learn seasonal patterns such as autumn storms,
   construction season and holidays.

**Prior evidence on (2):** the 2026-09-26 model search already tried time of year on the old,
uncorrected labels. The otherwise identical model *with* it did worse (validation MAE 2.660 vs
2.590, within 5 min 86.9% vs 87.8%), so the current models leave it out. The likely reason is
that training covers 2025-11-03 to 2026-08-21, so no calendar day appears twice, and the
September evaluation days fall on days of the year the model never saw. This change retests it
on corrected labels. Expect a gain only if the model picks up a broad seasonal trend rather than
memorizing individual days.

## What Changes

- New experiment directory `data/ml/recency_training_v3_20261014`, with three conditions trained
  on identical corrected data, settings and evaluation rows:
  - `control`: no weighting, no time of year (rerun so all three share one script version)
  - `strong`: the last month ramps from 1× up to **3×** (amplitude 2.0); older data counts 1×
  - `seasonal`: no weighting, plus the `doy_sin`/`doy_cos` time-of-year inputs
- Each condition trains the same three views as before (fine, ±3-min window, discrete), runs the
  ONNX and browser checks, and is scored on the same 4.24 million September arrivals.
- The report compares `strong` and `seasonal` each against `control`, with a plain-language
  summary at the top.
- `CURRENT` is unchanged. Any replacement is a separate change that needs the user's approval.

## Capabilities

### New Capabilities

### Modified Capabilities
- `delay-model`: the recency comparison accepts more than one variant condition.

## Impact

- `scripts/train_recency_delay.py`: a `--condition` choice of `control | weighted | strong |
  seasonal`, mapped to (amplitude, input trial)
- `scripts/recency_provenance.py`: reads the input trial from the run key instead of a fixed
  constant
- `scripts/run_recency_training.py`, `scripts/check_recency_discrete_delay.py`,
  `scripts/summarize_recency_training.py`: loop over the experiment's conditions and compare each
  variant with control
- Compute: about 27 min per condition, about 1.5 h in total, using the same memory caps
- The finished `recency_training_mixed_20261014` runs are not touched
