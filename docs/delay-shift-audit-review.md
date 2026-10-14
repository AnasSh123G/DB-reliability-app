---
kind: review
title: "Delay shift audit script: independent review"
---

Reviewed `scripts/investigate_delay_shift.py` against the current August 1–31-only comparison specification and source/schema. The audit job was already running; I did not poll or stop it, read datasets, or execute code. The only new baseline considered here is the previous calendar month, August 1–31.

## Findings

### P1 — The “immutable” manifest cannot detect changed input contents

**Location:** `scripts/investigate_delay_shift.py:16–18, 46–50, 186`.

`identity()` records only path, byte size, and mtime for the SQLite database, prepared binary columns, and reference predictions. Equal-size edits can preserve those values, and SQLite WAL commits can change visible database contents without changing the main database file's size/mtime; the dataset builder itself documents this limitation. The final database check repeats the same weak stat comparison. Consequently, a database or prepared input can change during the audit without invalidating the run, while the manifest still appears to freeze its identity.

**Action:** use content fingerprints for the immutable inputs, computed in bounded chunks (or reuse the dataset's database-content fingerprint and saved artifact hashes). Recheck those fingerprints before publishing `result.json`; include the prediction and ordered-ID content identity, not just the `.npz` stat.

### P1 — The reported “final common stop” can be an interior stop

**Location:** `scripts/investigate_delay_shift.py:174–175`.

`last_i` is the highest-sequence stop in `ci`, where `ci` includes only September 15 stops with at least one eligible August observation. If the actual final stop has no August match, the script silently selects the last earlier common stop and reports its deviation as `final_common_stop_deviation_min`. That can make a corridor stop look like the trip's terminal delay.

**Action:** identify the September 15 final stop from the full run path/stop sequence, and emit a final-stop deviation only when that exact stop has August support. Otherwise report it as unavailable; keep the last-common-stop metric under a separate explicit name if it is useful.

### P1 — The cause comparison treats every trip without a qualifying works flag as the comparison group

**Location:** `scripts/investigate_delay_shift.py:133–153, 175, 185`.

The `valid_works_trip == False` cohort includes trips with valid disruption notices, recorded messages whose validity is missing or non-overlapping, deleted/latest versions, and trips with no recorded message. `any_recorded` is collected but not used to form separate cohorts. The result key `no_verified_valid_works` can therefore be mistaken for a no-cause control even though it only means “no qualifying works flag.”

**Action:** report distinct cohorts for qualifying works, qualifying disruption, recorded-but-no-qualifying-active-notice, and no recorded message; label the last group as “no recorded message,” never “no cause.” Preserve the current retrospective-only limitation because receipt time is not proven.

### P2 — Only exact train-ID/path matches are attempted; verified cross-identity matches are omitted

**Location:** `scripts/investigate_delay_shift.py:96–100, 107–120, 185`.

The September cohort is keyed only by `train_id`, and an August run is discarded unless source and `path_sha1` match exactly. No route/source/run identity check is attempted for a changed train ID or path, although the specification asks to report exact matches separately from verified cross-identity matches. This is a conservative exact-match cohort, but the output currently does not provide a cross-identity audit or quantify which unmatched rows could be resolved with source evidence.

**Action:** retain the exact cohort as the primary August-only comparison, and add a separate unmatched/cross-identity audit using source run identity and route evidence. Only promote individually verified pairs; do not fold uncertain matches into the August mean.

### P2 — Scheduled-time deltas can be weekday/service-pattern variation, not timetable changes

**Location:** `scripts/investigate_delay_shift.py:120–124`.

Each August scheduled arrival is compared with the single September 15 scheduled timestamp for that stop, and a difference over one minute is counted as `scheduled_time_changed_over_1_min`. August includes multiple weekdays and service patterns, while September 15 is one Tuesday. Ordinary calendar/service-pattern variation can therefore inflate this “changed” count even when no timetable revision occurred. This does not justify adding another delay baseline; it limits what the schedule-difference count can establish.

**Action:** describe this as a raw scheduled-time delta against September 15, and avoid calling it a schedule change without independent feed/version evidence. Keep the previous-month mean as the sole delay baseline.

## Checks that passed

- The August comparison uses one vote per qualifying service date: it computes the mean across eligible matched stops for each run date, requires 80% of the fixed September/eligible-August common stop set, then averages the qualifying August date means. The deviation, SD, median, and p90 are based on those daily run means.
- Eligibility follows the arrival population's key filters in `dataset._where`: recorded quality, non-cancelled arrival, decidable holiday, scheduled arrival, present delay, and the two-hour plausibility bound. September prepared rows are checked against database labels and stop IDs.
- Stop matching requires recurring train ID, source, exact path, stop sequence, and station ID, preventing a repeated station or changed path from being silently joined by station name alone. Feed-version and scheduled-time differences are counted for the exact matched cohort.
- Cause versions with a non-null message ID are deduplicated to the latest source timestamp per trip/stop/message ID (null IDs remain separate); explicit validity bounds are required for active overlap, and trip-level versus stop-level flags are kept separately. These are retrospective associations, not receipt-time availability.
- The script uses read-only SQLite, a private memory cap and zero-swap guard, bounded prepared-data maps, and memmaps for the stop-by-date matrix. It writes outputs only under the repository's versioned audit directory.

## Remaining specification coverage

The script emits CSV/JSON summaries, but not the requested charts, route-level summaries, or paired trip/date-cluster uncertainty summaries. Its early-label examples are selected from negative-label rows only, so they do not provide matched normal controls. Treat these as missing deliverables if this script is expected to complete the full investigation, rather than as evidence that the corresponding checks passed.
