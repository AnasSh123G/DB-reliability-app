# Spec Delta

## ADDED Requirements

### Requirement: Every call shows the model's prediction alongside the stored average
Wherever the second interface shows a call's stored average (search results, a train's route, a
station board, a line's runs), it SHALL also show the current model's prediction for that same
call, computed in the browser. Both SHALL be visibly distinguishable as separate values, not
merged into one.

#### Scenario: Both values are shown together
- **WHEN** a call has both a stored average and a model prediction available
- **THEN** the second interface shows both, clearly distinguished, for that call

#### Scenario: Only the average is available
- **WHEN** a call has no model prediction (the model has nothing to run inference with, or
  inference has not finished loading yet)
- **THEN** the second interface shows the average alone, the same way it shows an average alone
  today when no model exists at all

#### Scenario: Only the model prediction is available
- **WHEN** a call has a model prediction but no stored average (its stop is new, or outside the
  averages' history)
- **THEN** the second interface shows the model's prediction alone

#### Scenario: Neither is available
- **WHEN** a call has neither
- **THEN** the second interface shows its existing "no average" state, unchanged
