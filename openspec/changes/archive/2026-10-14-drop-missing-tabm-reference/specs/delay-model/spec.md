## REMOVED Requirements

### Requirement: Replacement depends on per-arrival evidence and deployment approval
**Reason**: It required scoring the TabM artifact `20260924T183944Z` in every report, and that artifact no longer exists.
**Migration**: Replaced by "Replacement depends on per-arrival evidence and user approval", identical except for the TabM clause and its scenario.

## ADDED Requirements

### Requirement: Replacement depends on per-arrival evidence and user approval
There SHALL be no required improvement margin against flat or hierarchical historical averages.
The report SHALL present candidate and incumbent errors against actual arrivals on the same test
instances, including whether average absolute error improved, alongside the within-1/3/5-minute
shares. The report SHALL NOT promise a minimum achievable accuracy. An experimental artifact
SHALL leave `CURRENT` unchanged until browser size, compatibility and prediction-parity checks
pass and the user approves deployment.

#### Scenario: Research yields no convincing improvement
- **WHEN** candidate results do not establish a worthwhile improvement in individual-arrival
  accuracy or the candidate fails deployment checks
- **THEN** the report records the outcome and the current artifact remains in use

#### Scenario: A replacement is approved after evaluation
- **WHEN** the user approves a complete, validated replacement after reviewing its results
- **THEN** the pointer moves atomically, preserving the previous artifact for rollback
