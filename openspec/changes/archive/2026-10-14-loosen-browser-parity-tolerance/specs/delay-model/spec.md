## ADDED Requirements

### Requirement: Browser predictions match the Python reference within 0.01 minutes
An exported model's browser prediction-parity check SHALL run the model in the pinned browser
runtime and compare every forecast with the Python ONNX reference on the same fixture rows. It
SHALL pass only when, for every required case:
- the largest absolute difference from the Python reference is at most 0.01 minutes
- the largest difference between one-at-a-time and batched browser predictions is at most
  0.001 minutes
- the case has at least one row, no output of the wrong shape, and its unknown-category handling
  was checked

The required cases SHALL be median, ±3-minute window and ±5-minute window for a probability
output, and median alone for a quantile output. Other diagnostic numbers MAY be recorded but SHALL
NOT decide the result.

#### Scenario: Floating-point noise passes
- **WHEN** browser forecasts differ from the Python reference by at most 0.0045 minutes
- **THEN** the parity check passes

#### Scenario: An export bug fails
- **WHEN** any browser forecast differs from the Python reference by more than 0.01 minutes, or a
  difference is missing or not a finite number
- **THEN** the parity check fails

#### Scenario: A required case is missing
- **WHEN** a probability-output report lacks its median, ±3-minute or ±5-minute case
- **THEN** the parity check fails
