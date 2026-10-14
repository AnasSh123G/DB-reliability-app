---
kind: review
title: "Delay recency and cause specification: independent review"
---

Reviewed `docs/delay-recency-and-cause-spec.md` against the ML feature builder, cause-rate derivation, message/poll schema, recurring train identity and fit code. This was a bounded, read-only source review; I did not query data, run training, or execute an audit.

## Findings

### P1 — Existing notice-rate folds look ahead for training rows

**Location:** specification Step 3 and Step 4; `src/dbrel/ml/notice_rates.py:161–189`.

The specified recency-only comparison preserves the current notice-rate inputs. Their training fold removes a row's whole calendar month, but keeps counts from every other training month, including dates after that row's service date. For example, a July training row can receive a rate containing August notices (the same calendar month is excluded). This is not future information relative to the held-out September forecast, but it is look-ahead relative to the historical training row and can make the learned rates an unrealistic description of what was knowable then. A recency result therefore cannot by itself support a claim that model adaptation to past causes is point-in-time realistic.

**Action:** keep the fixed-feature recency comparison if the goal is to isolate weighting, but label its conclusion as conditional on the existing retrospective folds. Before claiming causal recent-history benefit or fitting recent cause aggregates, add a separate strictly-as-of-row-date feature audit/ablation, or rebuild those features from only earlier dates. Do not combine that fold change with the initial weighting comparison.

### P1 — “Has a cause” currently overstates active, stop-scoped notice evidence

**Location:** specification Step 3, “Retrospective explanation”; `src/dbrel/derive/notice_rates.py:40–44, 58–67`; `src/dbrel/models.py:691–711`.

`tt_message` versions are keyed by trip, stop sequence, message ID and source timestamp. The current notice builder treats any non-deleted type-`h` construction/disruption version as a trip-level flag; it does not check whether the validity interval overlaps a particular arrival, collapse a later deletion/version into an as-of state, or limit the flag to the linked stop. Thus a cause-versus-no-cause comparison can count stale/deleted messages and broadcast one stop's note to every arrival on the trip. The spec calls for version inventory and explicit propagation scope, but does not make those rules a prerequisite for the comparison.

**Action:** define and report at least two evidence levels separately: a version-deduplicated trip-level notice with validity overlapping the run, and a stop-linked notice with validity overlapping that stop's scheduled/actual window. Keep “no recorded cause” distinct from “verified no active notice,” and do not use the existing `notice_rate_daily` trip flags as verified event labels.

### P2 — Receipt-time evidence needs a concrete retained-payload source

**Location:** specification “Availability gate for predictive features”; `src/dbrel/models.py:640–662, 691–711`.

The gate correctly rejects `ts <= cutoff` as proof of availability. In the schema, however, `tt_poll` stores fetch time and a content hash but no payload or message foreign key, and `tt_message` stores no first-observed time. The instruction to inspect retained raw poll content is therefore contingent on an external archive whose location and linkage are not specified. A content hash alone cannot prove which message version a poll contained.

**Action:** name the retained payload archive and the deterministic join from a poll/hash to the exact message version, if one exists. If it does not, record historical direct-cause availability as unprovable and keep direct same-trip causes retrospective; do not infer receipt from a nearby poll.

## Checks that passed

- September 15's August 1–31 reference is explicitly descriptive, flags overlap with August 23–31 selection data; the user subsequently clarified that August 1–31 is the sole comparison baseline. The later September replay also freezes its epoch budget before September 15 and labels that date hypothesis-informed rather than independent proof.
- Recurring-trip matching does not rely on display names: it starts from train ID plus source, verifies path/direction and stop occurrence, and requires evidence for cross-identity matches. This matters because numbered timetable train identity can persist when route path changes (`src/dbrel/identity.py`; `src/dbrel/ingest/runs.py:290–304`).
- The two-fit/three-output constraint is coherent: one binned classifier can be scored under both median/CDF-quantile and window decision rules. The spec correctly counts both outputs as candidates and warns that they are dependent.
- The weight schedule keeps each trip's arrivals together, preserves the per-arrival unit, excludes validation/test data from weight construction, and requires a weighted numerator/denominator across batches. The report also calls for testing uniform-weight reduction and shuffle alignment.
