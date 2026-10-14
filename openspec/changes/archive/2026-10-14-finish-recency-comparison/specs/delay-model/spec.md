## ADDED Requirements

### Requirement: The recency comparison ends in a user decision
The recency comparison SHALL score control and recency-weighted models on the same eligible
arrivals, and report for each the within-1/3/5-minute shares and the average absolute error. Its
summary SHALL state in plain words whether weighting helped. `models/v2/CURRENT` SHALL remain
unchanged unless the user approves a replacement after reading that summary.

#### Scenario: Weighting does not help
- **WHEN** the weighted models are not more accurate than control on the same arrivals
- **THEN** the report says so and `CURRENT` stays unchanged

#### Scenario: Weighting helps
- **WHEN** a weighted model is more accurate and passes the browser checks
- **THEN** the user is asked whether it replaces `CURRENT`, and nothing changes before the user
  answers
