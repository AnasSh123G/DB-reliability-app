---
kind: spec
title: "September 15 versus August: early-label and delay-cause investigation"
---

# Investigation results before fitting

**24,252 supported recurring trips** were compared with their **August 1–31 average only**. Mean September 15 deviation: **+0.60 minutes**; median **-0.40 minutes**. Shares exceeding their August mean by 5 / 15 / 30 minutes: **10.42% / 2.96% / 0.75%**.

![August-only comparison](../data/ml/delay_shift_audit_20261013/august-deviation.png)

## Cohort and matching

September 15 has 337,845 eligible prepared arrivals across 38,354 recurring trips. 31,269 trips have qualifying August observations; 24,252 have at least ten August run dates. Matching requires identical recurring train ID, source, route path, station and stop sequence. Each August run mean gets one vote, provided at least 80% of the fixed common stops are observed. Stops without August matches are excluded explicitly; no identity is merged by name.

These restrictions improve comparability but exclude changed-route and changed-ID services, which could themselves be affected by works. No cross-identity pair has yet been independently verified. Results describe the matched population, not all September trips. An absent or cancelled arrival is not assigned zero delay. Service-pattern and weekday differences remain limitations; there is no weekday-only or partial-August delay baseline.

A descriptive 95% bootstrap interval for the matched-trip mean is [+0.52, +0.68] minutes, resampling whole recurring-trip deviations (1,000 deterministic draws). This keeps each trip’s stops together, but does not account for shared region-wide events or correlated August dates; it is not a causal/generalization confidence interval.

| Group | Trips | Mean deviation (min) | Median deviation (min) |
| --- | --- | --- | --- |
| FV | 321 | 2.45 | -1.35 |
| RV | 17684 | 0.49 | -0.43 |
| S | 6247 | 0.81 | -0.36 |

## Munich September 15 hotspots

| Station | Supported train-stop pairs | Mean deviation (min) | Median deviation (min) |
| --- | --- | --- | --- |
| München Ost | 503 | 4.39 | 0.85 |
| München Rosenheimer Platz | 406 | 4.85 | 1.53 |
| München Karlsplatz | 402 | 4.92 | 1.54 |
| München Hbf (tief) | 399 | 4.99 | 1.50 |
| München Hackerbrücke | 400 | 5.28 | 1.64 |
| München Donnersbergerbrücke | 608 | 4.40 | 1.20 |

These station numbers average supported recurring train-stop deviations; they are not independently sampled trips or a second baseline.

## Highest supported station deviations

| Station | Supported train-stop pairs | Mean deviation (min) | Median deviation (min) |
| --- | --- | --- | --- |
| München St.Martin-Str. | 84 | 19.51 | 3.04 |
| Bindlach | 33 | 16.42 | 6.92 |
| München-Fasangarten | 55 | 16.02 | 6.43 |
| Ramsenthal | 33 | 15.91 | 7.00 |
| Essen Dellwig Bf | 39 | 15.66 | 11.64 |
| Essen Bergeborbeck Bf | 39 | 15.50 | 11.58 |
| Harsdorf | 33 | 15.50 | 7.70 |
| München-Giesing | 82 | 15.45 | 1.72 |
| Ludwigschorgast | 31 | 15.32 | 4.96 |
| Essen Altenessen Bf | 39 | 15.32 | 11.74 |
| Trebgast | 35 | 15.25 | 10.46 |
| Essen Zollverein Nord Bf | 40 | 15.01 | 12.33 |
| Rövershagen Karls Erlebnisdorf | 30 | 14.43 | 13.37 |
| Graal-Müritz Koppelweg | 31 | 13.09 | 8.87 |
| Rostock Torfbrücke | 32 | 13.01 | 9.30 |

## Early-arrival label audit

All **43,415** prepared targets below −5 minutes on September 6–19 match the stored arrival-delay seconds and the reconstructed actual-minus-scheduled timestamps. Prepared/database mismatches: **0**; timestamp/delay mismatches: **0**; missing actual timestamps: **0**.

| Threshold | Arrivals |
| --- | --- |
| Below −5 minutes | 43,415 |
| Below −15 minutes | 23,097 |
| Below −30 minutes | 16,645 |
| Below −60 minutes | 7,506 |

The raw plan/change reconciliation inspected **282 negative RE 161xx stops** on September 15. **0** have both the database’s scheduled timestamp in a raw plan and its actual timestamp in a raw change response. Source hashes and timestamp values are retained in the reconciliation CSV; the concrete proposed replacements are in [proposed-label-reconciliation.csv](../data/ml/delay_shift_audit_20261013/proposed-label-reconciliation.csv). In this sample all 282 actual times agree with raw changes, while none of the stored scheduled times agrees with the unique raw planned time. Using the raw plans changes the mean from −63.56 minutes to +10.07 minutes; none remains earlier than −5 minutes. This identifies a concrete schedule/label discrepancy, rather than a model prediction problem. The source path pins station-pattern times when a sequence first appears (`ingest/runs.py`) and later resolves trip schedules from that reusable pattern (`changes_poller._load_patterns`), without a per-service-date planned-time snapshot. That mechanism can retain obsolete times when the same route operates on a revised timetable. No source facts or labels have been changed; correction scope must cover training and evaluation consistently, not just these discovered misses. **None of the 282 raw plans was observed in the retained archive before its corrected five-hour forecast cutoff.** They substantiate the retrospective schedule discrepancy, but cannot be inserted into forecast-time features as though their receipt before cutoff were proven. Approved correction policy must distinguish target reference schedules from point-in-time inputs; missing availability remains explicit.

## Construction and delay-cause evidence

| Retrospective cohort | Supported trips | Mean deviation (min) | Median deviation (min) |
| --- | --- | --- | --- |
| works valid on trip | 2177 | 0.88 | -0.49 |
| disruption valid, no works | 2774 | 2.32 | -0.24 |
| recorded message, no qualifying works/disruption | 3664 | 0.67 | -0.37 |
| no recorded message | 15637 | 0.24 | -0.43 |

These cohorts collapse versions using source timestamps, exclude latest deleted notices and require explicit validity overlap. Trip-wide overlap and stop-linked overlap are recorded separately. They are observational summaries of retained messages, not proof of works causing delays; no recorded message does not mean no disruption. Missing validity, unknown messages and overlapping cause families remain visible. Existing historical notice flags are not used as verified active-event labels.

### Raw archive and forecast-cutoff check

The targeted cohort includes 4,632 stops at six Munich hotspots and RE 161xx services on September 15. 4,632 appear in retained September 14–16 responses. Works notes appear at some point for **475** stops; **434** have a non-deleted, validity-overlapping works note observed in the archive before the **stored** scheduled arrival minus five hours. This must be rechecked against corrected trip-specific schedules before predictive use.

Archive recording timestamps supply a concrete route to historical receipt evidence. Coverage remains targeted and does not certify national direct-cause feature availability or delivery to the running app. Earlier source observations outside the scanned partitions may exist. Same-instant conflicting message versions require additional ambiguity checks before predictive use. No direct cause feature has been added.

## What the experiment can test next

Before fitting, resolve the confirmed trip-specific schedule mismatch. The proposed correction is to preserve raw per-trip planned-time snapshots with source recording timestamps, recompute delay labels against the approved schedule policy, and regenerate both training and evaluation features/labels consistently. Keep original facts/cache versions and report the correction effect separately. Until that policy is approved, labels remain unchanged and no fit begins. Use a July 22–August 22 training-date ramp from 1 to 1.5, with unweighted evaluation. Fit only the fine-tuned MLP and one classifier, producing the three selected candidate views. Record that existing leave-one-month-out historical notice folds use other training months, including dates after some historical training rows. The initial recency comparison is conditional on those retrospective folds; it does not validate point-in-time cause adaptation. A new recent-cause feature needs a separate strictly-past feature audit.

If the September change arose after the August 22 training cutoff, that ramp cannot learn it. A later chronological replay would need matched weighted/unweighted controls trained on the same later history, and remains a separately reported diagnostic experiment. September 15 and the later validation are reused, hypothesis-informed dates.

## Coverage and provenance limitations

| Date | Eligible arrivals | Exact matched stop rows | Matched trips with validity-overlapping works |
| --- | --- | --- | --- |
| 2026-08-01 | 264,656 | 118,303 | 3360 |
| 2026-08-02 | 248,008 | 107,244 | 3194 |
| 2026-08-03 | 287,375 | 192,919 | 208 |
| 2026-08-04 | 291,869 | 188,704 | 3686 |
| 2026-08-05 | 298,184 | 187,264 | 3785 |
| 2026-08-06 | 280,572 | 186,921 | 4103 |
| 2026-08-07 | 302,620 | 187,010 | 3992 |
| 2026-08-08 | 254,003 | 118,436 | 3149 |
| 2026-08-09 | 239,216 | 106,838 | 2962 |
| 2026-08-10 | 301,877 | 214,064 | 3535 |
| 2026-08-11 | 304,224 | 217,754 | 41 |
| 2026-08-12 | 303,357 | 214,886 | 3633 |
| 2026-08-13 | 299,062 | 217,588 | 3618 |
| 2026-08-14 | 309,429 | 216,831 | 3770 |
| 2026-08-15 | 261,970 | 136,976 | 3059 |
| 2026-08-16 | 248,704 | 126,245 | 2465 |
| 2026-08-17 | 306,121 | 236,786 | 3371 |
| 2026-08-18 | 294,670 | 212,220 | 3433 |
| 2026-08-19 | 301,484 | 212,662 | 3280 |
| 2026-08-20 | 299,948 | 219,203 | 3446 |
| 2026-08-21 | 301,937 | 217,711 | 3228 |
| 2026-08-22 | 256,960 | 135,199 | 3041 |
| 2026-08-23 | 241,583 | 122,933 | 2691 |
| 2026-08-24 | 305,386 | 220,728 | 54 |
| 2026-08-25 | 307,728 | 240,314 | 143 |
| 2026-08-26 | 281,948 | 233,665 |  |
| 2026-08-27 | 195,619 | 214,210 |  |
| 2026-08-28 | 149,649 | 199,570 |  |
| 2026-08-29 | 241,658 | 142,465 |  |
| 2026-08-30 | 249,210 | 125,401 |  |
| 2026-08-31 | 235,552 | 259,353 |  |
| 2026-09-06 | 254,765 | 139,815 |  |
| 2026-09-07 | 307,081 | 280,708 |  |
| 2026-09-08 | 319,648 | 283,806 |  |
| 2026-09-09 | 308,324 | 273,931 |  |
| 2026-09-10 | 309,527 | 274,932 |  |
| 2026-09-11 | 304,828 | 264,014 |  |
| 2026-09-12 | 272,970 | 161,187 |  |
| 2026-09-13 | 251,429 | 145,393 |  |
| 2026-09-14 | 285,527 | 319,240 |  |
| 2026-09-15 | 337,845 | 337,845 |  |
| 2026-09-16 | 341,873 | 325,577 |  |
| 2026-09-17 | 336,852 | 324,142 |  |
| 2026-09-18 | 329,338 | 317,753 |  |
| 2026-09-19 | 281,211 | 186,860 |  |

The initial streaming run records source file metadata and validates reference ordered IDs and labels. Metadata is weaker than content fingerprints, especially for SQLite WAL changes; final publication content hashes and a read-snapshot normal-control/terminal check are recorded separately. These do not retroactively prove that the original stream saw no transient edits. Some daily works counts were lost in the CSV-export failure and are shown as missing; all saved stop means were reconciled against the completed 45-date matrix during recovery. The initial run’s highest common observed stop is a last-common-stop metric, not a verified terminal. The separate terminal CSV resolves true final sequences and reports missing August terminal support explicitly. Scheduled time-of-day differences against September 15 are descriptive differences, not proof of timetable revisions. Cross-identity matching, verified physical early arrivals and nationwide cause receipt remain unresolved.

Initial audit peak: 1501.9 MiB; raw scan peak: 1245.1 MiB. Both use private 2 GiB limits and zero job swap. No fitting has started; no production pointer, endpoint or source fact was changed.

[Per-trip averages and deviations](../data/ml/delay_shift_audit_20261013/trip-comparison.csv) · [Per-stop averages and deviations](../data/ml/delay_shift_audit_20261013/stop-comparison.csv) · [Early examples](../data/ml/delay_shift_audit_20261013/early-label-examples.csv) · [Raw source traces](../data/ml/delay_shift_audit_20261013/raw-early-plan-reconciliation.csv) · [Cause inventory](../data/ml/delay_shift_audit_20261013/cause-inventory.csv) · [Raw forecast-cutoff coverage](../data/ml/delay_shift_audit_20261013/raw-receipt-stop-coverage.csv)

[Verified raw-plan summary and content identities](../data/ml/delay_shift_audit_20261013/raw-plan-validation.json) · [Exact raw-plan receipts and source hashes](../data/ml/delay_shift_audit_20261013/raw-plan-source-evidence.csv) · [Publication fingerprints and normal/terminal controls](../data/ml/delay_shift_audit_20261013/publication-verification.json)
