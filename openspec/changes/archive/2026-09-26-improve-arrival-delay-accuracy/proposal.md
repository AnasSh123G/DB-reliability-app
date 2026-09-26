# Proposal

## Why

The user needs a prediction for one particular train arriving at one particular station and
scheduled time, potentially five hours ahead, and reports that TabM meets a three-minute
tolerance on only about 75–80% of tested instances. This change investigates better compact
models and statistical formulas, judging every prediction against that arrival's actual delay,
not against a historical mean or an aggregated target.

## What Changes

- Put a research-and-selection checkpoint before candidate implementation or expensive training:
  investigate arrival-only TabM settings, alternative compact tabular networks, and statistical
  approaches that condition on the requested call. A hierarchical estimator is one possibility,
  not a required baseline or predetermined solution.
- Evaluate individual arrivals using absolute error and the percentage within 1, 3 and 5 minutes,
  with **within 3 minutes** primary. Retain individual prediction/outcome rows, coverage and
  breakdowns by date, train segment and day type; never replace actual outcomes with group means.
- Compare candidates, including the incumbent TabM, on identical held-out arrival instances.
  Remove the mandatory averages-baseline comparisons and the margin-over-averages promotion gate.
  Select using validation instances, report the final test honestly, and obtain approval before
  changing the deployed artifact. No accuracy improvement is promised in advance.
- Preserve advance-planning semantics: inputs, historical fitting data and preprocessing must be
  available before the forecast cutoff. Do not feed in the target run's later observed delays.
- Research local RX 9070 XT/Linux training support before neural training; inspect the existing
  isolated ROCm environment and verify correctness before considering replacement or a build.
- **BREAKING, upon an approved replacement:** the new predictor serves arrival estimates only.
  Preserve the old artifact and its serving compatibility until then; departure stored averages
  and database observations remain intact.
- Keep inference client-side and prediction assets within 35 MB. Investigate export/runtime
  feasibility before full training, including compact coefficients/tables for a formula.
- Focus on recurring patterns, not incident prediction. Keep valid exceptional delays in the
  headline evaluation; any separately reported subset must disclose its rule and size.

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `delay-model`: research-backed candidate selection, per-arrival truth and tolerance scoring,
  advance-forecast validity, arrival-only replacement artifacts and a browser size constraint.

## Impact

- **Evaluation:** `src/dbrel/ml/{dataset,features,split,metrics,bakeoff,report}.py` and their tests;
  `src/dbrel/study/live_accuracy.py` is a reference for existing individual-outcome scoring.
- **Candidate-specific implementation:** training scripts, candidate adapters, export and browser
  integration are finalized only at the research checkpoint; no new dependency is assumed.
- **Data:** no database schema or production averages changes. Recorded arrivals remain individual
  targets; historical data is read-only. Existing cached datasets and model artifacts are preserved.
- **Compatibility:** assess CLI, browser and live-study consumers before shipping a different
  output contract. If study changes are needed, propose that explicit scope at the checkpoint
  rather than knowingly leaving it broken.
- **Current status:** the research audit and bounded shortlist are documented and approved for
  experiments. No implementation, training or deployment has begun.
