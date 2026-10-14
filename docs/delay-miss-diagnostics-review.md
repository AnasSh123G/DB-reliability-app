---
kind: review
title: "Delay miss diagnostics: independent review"
---

Reviewed `scripts/analyze_delay_misses.py` as a read-only diagnostic before recency weighting or retraining. The review covered bounded reads, saved prediction alignment, the strict miss threshold, raw/displayed scoring, vocabulary and database lookup, group statistics and rankings, and station/day excess calculations.

## Finding

### P2 — Candidate prediction files are not tied to an ordered row identity

**Location:** `scripts/analyze_delay_misses.py:99–102`, with downstream group comparisons at `190–196`.

The reference archive is checked against prepared `stop_id`s in blocks, but each candidate `.npy` is checked only for shape. Matching the frozen overall, per-day, and per-segment metrics catches many errors, but does not prove that a candidate value remains paired with its original arrival. A permutation within one day and segment can leave all those frozen scores unchanged while changing station, train, hour, novelty, and station/day miss attribution.

For example, with two rows in the same day and segment, `y=[0,0]`, and station groups `[A,B]`, points `[0,6]` and `[6,0]` have the same overall, daily, and segment metrics. They assign the six-minute miss to different stations. The current candidate checks accept the same row count, and the available frozen per-day/per-segment summaries cannot distinguish those arrays.

**Action:** persist an ordered row-identity companion for each candidate prediction file (or an equivalent block-verifiable identity binding written with the predictions), and verify it against the prepared `stop_id` sequence before computing row-level group diagnostics. Retain the existing shape and frozen-score checks as separate consistency guards.

## Checks that passed

- The comparison uses `abs(y - point) > 5`, so exactly five minutes is not counted as a miss. Underestimation and overestimation signs are consistent with `y - point`.
- Raw scores use the stored point. Displayed scores apply the shared app rule through `shown()`. Counts, miss rates, miss shares, MAE, and errors over 15 minutes use the expected per-group denominators.
- Rate rankings enforce the configured minimum support (1,000 by default and 250 for trains); count rankings remain separate and deterministic on ties.
- One-based category slots decode through `vocab[slot - 1]`, with slot zero labeled unknown. Database lookups are read-only and batched in groups of 400. The script samples prepared station slots against `trip_stop.station_id` before grouping.
- Station/day excess compares each station-day's displayed reference miss count with the rate on that station's other days, gated at 100 arrivals for the day and 1,000 on other days. The output notes that this is diagnostic, not causal. This calculation is intentionally for `reference_display`; candidate station/day excess is not produced.
- The main loop reads prepared columns in 131,072-row blocks, memory-maps candidate arrays, and checks that it is under a private 2 GiB cgroup with swap disabled. It materializes the compact reference IDs and points from the NPZ, but does not materialize the full feature columns or prediction arrays.
- Synthetic CPU checks of `Group`, support-filtered `ranks`, and 400-item-chunked SQLite `lookup` passed. No large dataset, model, or GPU was used.

## Report-only follow-up review — 2026-10-13

**Scope:** `scripts/report_delay_misses.py`, `docs/delay-miss-diagnostics.md`, and the completed aggregate `data/ml/delay_miss_diagnostics_20261013/result.json`. I read no datasets or models and ran no inference or jobs.

### P2 — The linked raw JSON still exposes unverified candidate spatial rankings

**Location:** `scripts/report_delay_misses.py:96`; candidate result groups in `result.json`.

The report prose, tables, and chart correctly select `reference_display`; the station plots read only the two `reference_display_*.csv` files. The document also explicitly says candidate location CSVs must not be used for conclusions until identity binding is added. However, the final “Raw diagnostic JSON” link points to the unfiltered JSON, which includes candidate station, train, line, day, and segment summaries for fine, discrete, and distribution outputs. Those candidate spatial results still lack verified ordered stop-ID alignment, as described in the finding above. The report’s baseline conclusions exclude them, but the linked artifact makes those unresolved attributions available alongside the report.

**Action:** remove that link or replace it with a reference-only/redacted aggregate export. Keep the candidate ordered-ID issue **unresolved for candidate spatial CSVs and candidate spatial summaries**; the current published report and chart exclude those results from their conclusions. Do not treat the baseline-only report as resolving candidate alignment globally.

### Checks that passed

- The headline reconciles to `reference_display`: 4,241,218 stop-arrivals, 526,469 misses, miss rate 12.4132% (12.41%), and 439,336 underestimated misses (83.45%).
- The state and station concentration values recompute: DE-NW, DE-BY, and DE-BW contribute 53.79% of misses and 41.90% of arrivals; the ten stations with most misses contribute 3.79% of misses.
- All 14 daily arrival and miss counts match the document and sum to the headline totals. I independently recomputed each service-mix residual as `(observed day misses − expected day misses) / day arrivals`, where expected misses are the sum over S-Bahn, regional, and long-distance of `day segment arrivals × full-period reference segment miss rate`. The three segment arrival counts sum to each day's total, and every recomputed residual rounds to the published value. The chart's dashed series is observed rate minus that residual, which is the service-mix expectation. The stated limitation—that this standardizes only those three service proportions and does not adjust for geography, weekday, route, or disruptions—is accurate.
- The station/day plot and attribution use the verified reference CSVs, with the documented 50-arrival cell threshold. The report labels the spike comparisons as unadjusted and noncausal. The report source does not load candidate spatial CSVs for its calculations.
- The recency section is a proposal: unit weights for older training trips, a linear ramp to 1.5 over July 22–August 22, no validation contribution to weights, and unweighted evaluation. It also scopes the later comparison and calls for ordered IDs and hashes. The report-generation code does not implement weighting or training. The statement that no retraining has begun is a runtime/process claim and was not independently checked as part of this static, aggregate-only review.
- The report preserves relevant evaluation limits: these are reused exploratory validation dates, not an independent test or deployed final-refit evaluation; rows are stop-arrivals rather than independent trips; the early-arrival labels need auditing; and local spike comparisons do not establish causes.
