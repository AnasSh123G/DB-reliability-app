## ADDED Requirements

### Requirement: A candidate replaces CURRENT only after a fair head-to-head
Before `CURRENT` moves to a candidate artifact, the candidate SHALL be trained up to the same last
training date as the current artifact. Both SHALL be scored on the same arrivals that neither
trained on, each with its own input encoding and notice rates. `CURRENT` SHALL move only if the
candidate is ahead on both within-5-minute share and average absolute error on the labels the
report treats as correct, and the user approves. The deployed notice-rate totals SHALL match the
artifact's fingerprint before production serves it.

#### Scenario: Different training cutoffs
- **WHEN** the candidate's training data ends earlier than the current artifact's
- **THEN** it is retrained up to the current artifact's last date before the head-to-head

#### Scenario: The candidate does not win
- **WHEN** the head-to-head shows the candidate tied or behind on either measure
- **THEN** `CURRENT` stays unchanged and the result is reported to the user
