---
kind: review
title: "Review: September delay-shift investigation"
---

# Findings

1. **Resolved: the raw-plan replacement evidence is now attributable and reproducible.** The second pass records API-plan response timestamps, source run/stop/EVA, response hashes and partitions, verifies that every one of the 282 targets has exactly one source plan equal to the prior reconciliation set, and checks the changed-time match. The aggregate independently reports the −63.5638 to +10.0745 minute mean change, range 0–41, zero corrected targets below −5, and zero plan receipts before the corrected five-hour cutoff. This closes the prior uniqueness/provenance gap. The report correctly separates this retrospective target-reference finding from forecast-time feature availability; the 0-before-cutoff result must stay attached to that distinction.

2. **The 434 works-cutoff count still needs an explicit stored-schedule qualifier.** [audit_delay_raw_receipt.py](../scripts/audit_delay_raw_receipt.py:61) computes the five-hour cutoff and validity overlap from the stored scheduled timestamp. The current report at [line 85](delay-shift-investigation.md:85) says only “before scheduled arrival minus five hours,” even though the same report documents 282 schedule disagreements and says the 434 observations must not be treated as verified forecast-time inputs. Please say “stored scheduled arrival” there and retain the recheck-before-predictive-use condition. The raw-plan verifier’s zero-before-corrected-cutoff count applies to the 282 target plans, not automatically to the 434 works-note count.

3. **Raw source identities are now included, with a remaining hash/read race caveat.** `verify_raw_plan_evidence.py` hashes all scanned archive partitions and the raw evidence/reconciliation outputs, closing the earlier omission for the schedule and receipt evidence. It hashes each partition before reading it and does not compare size/mtime after the scan, so a concurrent mutation could make the recorded digest differ from the bytes actually parsed. If these identities are intended as snapshot evidence, add a post-read stability check or describe them as source fingerprints rather than a read snapshot. Keep the report’s explicit limitation that the original streaming run used stat metadata and cannot be retroactively proven stable by the later hashes.

## Review scope

Reviewed report and source logic, plus the small aggregate result, raw-plan reconciliation, report-summary, raw-receipt result, publication-verification JSON, and raw-plan-validation JSON. No database, full CSV/data artifact, model, or running job was opened or executed.
