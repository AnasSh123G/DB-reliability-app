---
kind: spec
title: "Delay misses by date and place — before recency retraining"
---

# Delay misses before retraining

Across **4,241,218 recorded stop-arrivals**, **526,469 miss by more than five minutes (12.41%; accuracy 87.59%). **83.45% of misses underestimate delay**.

This uses saved, training-only baseline predictions for **service dates September 6–19, 2026**, after the unchanged app clamp/round rule. The baseline was trained through August 22. These are reused exploratory validation dates, not a fresh independent test, and this is not an in-sample evaluation of the deployed final-refit model. Counts are stop-arrivals, not independent trips; many errors can belong to the same journey. A miss means absolute error strictly greater than five minutes.

![Daily and station miss rates](../data/ml/delay_miss_diagnostics_20261013/miss-patterns.png)

## When misses increase

| Group | Arrivals | Misses | Miss rate | Excess over service-mix expectation |
| --- | --- | --- | --- | --- |
| 2026-09-06 | 254,765 | 22,518 | 8.84% | -3.68 percentage points |
| 2026-09-07 | 307,081 | 36,145 | 11.77% | -0.58 percentage points |
| 2026-09-08 | 319,648 | 37,488 | 11.73% | -0.57 percentage points |
| 2026-09-09 | 308,324 | 40,121 | 13.01% | +0.63 percentage points |
| 2026-09-10 | 309,527 | 37,668 | 12.17% | -0.29 percentage points |
| 2026-09-11 | 304,828 | 39,262 | 12.88% | +0.30 percentage points |
| 2026-09-12 | 272,970 | 26,227 | 9.61% | -2.97 percentage points |
| 2026-09-13 | 251,429 | 22,282 | 8.86% | -3.74 percentage points |
| 2026-09-14 | 285,527 | 37,560 | 13.15% | +0.76 percentage points |
| 2026-09-15 | 337,845 | 53,809 | 15.93% | +3.56 percentage points |
| 2026-09-16 | 341,873 | 49,021 | 14.34% | +2.07 percentage points |
| 2026-09-17 | 336,852 | 47,838 | 14.20% | +1.90 percentage points |
| 2026-09-18 | 329,338 | 46,280 | 14.05% | +1.71 percentage points |
| 2026-09-19 | 281,211 | 30,250 | 10.76% | -1.72 percentage points |

The dashed chart line uses each day’s S-Bahn/regional/long-distance proportions and the corresponding full-period miss rates. The residual removes differences in those three proportions only; it does not control for geography, weekday, route or disruptions. Dates are service dates; overnight arrivals may occur on the following calendar day.

## Where misses concentrate

North Rhine-Westphalia, Bavaria and Baden-Württemberg together account for **53.79% of misses**, compared with **41.90% of arrivals**. However, the ten stations with most misses account for only **3.79% of all misses**. The problem is distributed across the network, with local spikes on top.

| Group | Arrivals | Misses | Miss rate |
| --- | --- | --- | --- |
| DE-NW | 549,909 | 107,247 | 19.50% |
| DE-BY | 761,448 | 106,680 | 14.01% |
| DE-BW | 465,654 | 69,280 | 14.88% |
| DE-HE | 335,190 | 42,379 | 12.64% |
| DE-NI | 206,696 | 41,480 | 20.07% |
| DE-BE | 712,300 | 27,828 | 3.91% |
| DE-ST | 134,334 | 23,833 | 17.74% |
| DE-SN | 231,693 | 22,814 | 9.85% |
| DE-RP | 147,365 | 20,885 | 14.17% |
| DE-HH | 209,083 | 18,333 | 8.77% |
| DE-BB | 154,316 | 12,004 | 7.78% |
| DE-TH | 123,472 | 11,193 | 9.07% |
| DE-SH | 88,621 | 11,071 | 12.49% |
| DE-MV | 67,518 | 5,812 | 8.61% |
| DE-SL | 34,864 | 2,914 | 8.36% |
| DE-HB | 17,007 | 2,538 | 14.92% |
| Unknown/cross-border | 1,748 | 178 | 10.18% |

### Stations with most misses

| Group | Arrivals | Misses | Miss rate |
| --- | --- | --- | --- |
| Köln Hbf [id 269] | 9,115 | 2,636 | 28.92% |
| Düsseldorf Hbf [id 113] | 8,514 | 2,524 | 29.65% |
| Nürnberg Hbf [id 354] | 7,891 | 2,034 | 25.78% |
| Dortmund Hbf [id 109] | 6,469 | 1,991 | 30.78% |
| Hannover Hauptbahnhof [id 186] | 5,102 | 1,872 | 36.69% |
| München Ost [id 9056] | 11,301 | 1,861 | 16.47% |
| Frankfurt (Main) Hauptbahnhof [id 141] | 5,972 | 1,834 | 30.71% |
| Duisburg Hbf [id 112] | 4,453 | 1,811 | 40.67% |
| Stuttgart Hbf [id 9498] | 5,833 | 1,715 | 29.40% |
| Hamburg Hbf [id 8616] | 5,141 | 1,685 | 32.78% |

### Stations with highest miss rates (at least 1,000 arrivals)

| Group | Arrivals | Misses | Miss rate |
| --- | --- | --- | --- |
| Essen Altenessen Bf [id 2294] | 1,360 | 696 | 51.18% |
| München Hbf [id 332] | 3,233 | 1,574 | 48.69% |
| Hamm (Westf), Hauptbahnhof [id 184] | 1,910 | 916 | 47.96% |
| Frankfurt(M) Flughafen Fernbf [id 8476] | 1,578 | 699 | 44.30% |
| Minden(Westf) [id 9009] | 1,279 | 544 | 42.53% |
| Gelsenkirchen Hbf [id 160] | 3,615 | 1,515 | 41.91% |
| Bitterfeld [id 40] | 1,898 | 773 | 40.73% |
| Duisburg Hbf [id 112] | 4,453 | 1,811 | 40.67% |
| Delitzsch unt Bf [id 1758] | 1,145 | 464 | 40.52% |
| Oberhausen Hbf [id 356] | 3,192 | 1,236 | 38.72% |

Station rates reflect service mix as well as place; long-distance hubs are not directly comparable with S-Bahn-only stops. UNKNOWN station categories merge unseen identities rather than identifying a physical station.

### Date-specific station spikes

| Service date | Station | Arrivals / misses | Miss rate | Other 13 days |
| --- | --- | --- | --- | --- |
| 2026-09-15 | München Donnersbergerbrücke | 898 / 271 | 30.18% | 10.16% |
| 2026-09-15 | München Ost | 780 / 282 | 36.15% | 15.01% |
| 2026-09-19 | Berlin Sonnenallee | 509 / 173 | 33.99% | 5.98% |
| 2026-09-16 | Stuttgart Feuersee | 717 / 211 | 29.43% | 10.12% |
| 2026-09-15 | München Karlsplatz | 630 / 195 | 30.95% | 9.06% |
| 2026-09-15 | München Hbf (tief) | 630 / 195 | 30.95% | 9.15% |
| 2026-09-15 | München Rosenheimer Platz | 630 / 191 | 30.32% | 9.01% |
| 2026-09-15 | München Hackerbrücke | 624 / 190 | 30.45% | 9.11% |
| 2026-09-15 | München Marienplatz | 631 / 190 | 30.11% | 9.07% |
| 2026-09-15 | München Isartor | 631 / 189 | 29.95% | 9.20% |

Several Munich stops spike together on September 15. Berlin Sonnenallee on September 19 and Stuttgart Feuersee on September 16 also stand out. These patterns are consistent with shared local events or systematic feed effects, but the statistics do not establish their causes. Each spike has at least 100 arrivals that day and at least 1,000 on other dates. Comparisons with the other thirteen days are unadjusted for weekday and service mix.

## Which cases are missed

| Group | Arrivals | Misses | Miss rate | Share of all misses |
| --- | --- | --- | --- | --- |
| Regional | 2,046,223 | 321,721 | 15.72% | 61.11% |
| Long-distance | 69,731 | 35,493 | 50.90% | 6.74% |
| S-Bahn | 2,125,264 | 169,255 | 7.96% | 32.15% |

| Group | Arrivals | Misses | Miss rate | Share of all misses |
| --- | --- | --- | --- | --- |
| Earlier than -5 min | 43,415 | 43,415 | 100.00% | 8.25% |
| -5 to -1 min | 136,850 | 4,720 | 3.45% | 0.90% |
| Exactly on time | 1,409,064 | 15,213 | 1.08% | 2.89% |
| 1–4 min late | 1,748,689 | 17,597 | 1.01% | 3.34% |
| 5–14 min late | 703,996 | 262,548 | 37.29% | 49.87% |
| 15–29 min late | 146,682 | 134,948 | 92.00% | 25.63% |
| 30+ min late | 52,522 | 48,028 | 91.44% | 9.12% |

Regional trains produce most misses, while long-distance trains have the highest rate. Late arrivals of 5 minutes or more account for most misses. This supports investigating delay propagation and disruption information; increasing recent training weights alone cannot supply an incident signal missing from the inputs.

**Early-arrival labels need an audit before drawing training conclusions.** There are 43,415 targets earlier than −5 minutes; the display’s nonnegative clamp necessarily misses them. Their mean absolute displayed error is 33.56 minutes. Several named regional services have 250–252 arrivals and a 100% miss rate dominated by overestimation, with average errors around an hour. This is a signal to inspect source timestamps, schedule versions and label construction, not evidence of a particular bug. No labels have been changed or removed.

| Group | Arrivals | Misses | Miss rate | MAE (minutes) |
| --- | --- | --- | --- | --- |
| RE 16103 [id 93451] | 252 | 252 | 100.00% | 68.64 |
| RE 16105 [id 96185] | 252 | 252 | 100.00% | 68.40 |
| RE 16113 [id 105596] | 252 | 252 | 100.00% | 64.60 |
| RE 16119 [id 112686] | 252 | 252 | 100.00% | 59.40 |
| RE 16121 [id 115134] | 252 | 252 | 100.00% | 61.69 |
| RE 16127 [id 122953] | 251 | 251 | 100.00% | 62.75 |
| RE 16129 [id 125328] | 250 | 250 | 100.00% | 55.71 |
| RE 16131 [id 127454] | 250 | 249 | 99.60% | 64.03 |

### Scheduled local hour

| Group | Arrivals | Misses | Miss rate |
| --- | --- | --- | --- |
| 00:00–00:59 | 8,570 | 1,785 | 20.83% |
| 01:00–01:59 | 3,821 | 832 | 21.77% |
| 02:00–02:59 | 6,417 | 1,006 | 15.68% |
| 03:00–03:59 | 15,818 | 1,310 | 8.28% |
| 04:00–04:59 | 66,830 | 4,523 | 6.77% |
| 05:00–05:59 | 175,823 | 13,340 | 7.59% |
| 06:00–06:59 | 234,945 | 22,763 | 9.69% |
| 07:00–07:59 | 256,233 | 26,743 | 10.44% |
| 08:00–08:59 | 176,922 | 20,723 | 11.71% |
| 09:00–09:59 | 213,028 | 23,807 | 11.18% |
| 10:00–10:59 | 224,013 | 25,940 | 11.58% |
| 11:00–11:59 | 233,766 | 27,334 | 11.69% |
| 12:00–12:59 | 235,594 | 28,658 | 12.16% |
| 13:00–13:59 | 239,167 | 29,538 | 12.35% |
| 14:00–14:59 | 244,728 | 32,554 | 13.30% |
| 15:00–15:59 | 246,374 | 33,204 | 13.48% |
| 16:00–16:59 | 237,670 | 34,094 | 14.35% |
| 17:00–17:59 | 240,903 | 35,086 | 14.56% |
| 18:00–18:59 | 248,229 | 36,300 | 14.62% |
| 19:00–19:59 | 239,999 | 33,271 | 13.86% |
| 20:00–20:59 | 229,762 | 32,226 | 14.03% |
| 21:00–21:59 | 203,879 | 28,104 | 13.78% |
| 22:00–22:59 | 179,156 | 24,092 | 13.45% |
| 23:00–23:59 | 79,571 | 9,236 | 11.61% |

### Known and unseen identities

| Group | Arrivals | Misses | Miss rate |
| --- | --- | --- | --- |
| Known train / known station | 4,098,327 | 507,083 | 12.37% |
| Unknown train / known station | 142,231 | 19,199 | 13.50% |
| Known train / unknown station | 127 | 36 | 28.35% |
| Unknown train / unknown station | 533 | 151 | 28.33% |

Unseen train identities account for only about 3.68% of misses; they are not the main source of error. Line names can cover services in several regions, so line labels alone should not be interpreted as a geographic corridor.

## Reliability of the attribution

The reference prediction array was aligned to prepared stop IDs in full, aggregate scores were reconciled, and sampled station IDs were cross-checked against the read-only database. The independent review confirmed thresholds, grouping denominators and vocabulary decoding. Candidate arrays lack an ordered stop-ID companion: aggregate/day/segment checks cannot detect every within-group reordering. **This report and chart therefore use only the verified reference for all attribution.** Candidate location CSVs in the raw output directory are exploratory and must not be used for conclusions until identity binding is added.

Diagnostics peak memory: **706.7 MiB**, under a private 2 GiB limit with job swap disabled. No models were retrained and no app endpoints, deployment pointer or commits were changed.

## Next phase, after these findings are reviewed

Use weight 1 for older training trips, with a linear ramp from 1 to 1.5 over the last calendar month of the training split. For the unchanged split this is July 22–August 22; validation dates cannot define or enter the training ramp. Normalize by the sum of weights and leave evaluation unweighted.

Limit the comparison to fine-tuned MLP, discrete-quantile classifier output, and the ±3-minute-window classifier output. The two classifier outputs can share one trained network, yielding three candidates from two fits. Report raw and displayed metrics, the same date/place breakdown, and artifact size; preserve browser output and endpoint contracts. Add ordered stop-ID companions and prediction hashes for future candidate attribution.

The next investigation should also audit the systematically early labels and separate recurrent weaknesses from isolated date-specific spikes. No recency implementation or retraining has begun: this report is the requested checkpoint.

[Complete verified baseline station CSV](../data/ml/delay_miss_diagnostics_20261013/reference_display_stations.csv) · [Baseline station/date CSV](../data/ml/delay_miss_diagnostics_20261013/reference_display_station_day.csv) · [Verified reference diagnostic JSON](../data/ml/delay_miss_diagnostics_20261013/reference-only.json) · [Independent review](delay-miss-diagnostics-review.md)
