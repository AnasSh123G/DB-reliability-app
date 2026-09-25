# Spec Delta

## ADDED Requirements

### Requirement: The current model artifact is servable to a browser client
The system SHALL make the model artifact `CURRENT` points to retrievable over HTTP, and SHALL let
a client cache it indefinitely until `CURRENT` moves to a different artifact.

#### Scenario: Fetching the current artifact
- **WHEN** a client requests the current model artifact
- **THEN** it receives the same ONNX file bytes the export step verified against the trained
  weights, with a cache key that changes only when `CURRENT` moves

#### Scenario: No trained model exists
- **WHEN** no artifact has ever been exported
- **THEN** the request fails clearly, and nothing that depends on it (browser inference, or a
  scheduled call's encoded input) is silently given fabricated data

### Requirement: A scheduled call's model input can be computed without running inference
For any scheduled call the system can already build features for, the system SHALL be able to
produce that call's fully encoded model input — the categorical and numeric arrays the current
artifact's model expects — using the same feature and vocabulary code prediction uses, without
itself running the model.

#### Scenario: Encoded input matches what prediction would use
- **WHEN** a scheduled call's model input is computed this way, and separately through the
  existing offline prediction path
- **THEN** the categorical and numeric arrays are identical

#### Scenario: A call outside the model's vocabulary still gets an input
- **WHEN** a call's train, station or other categorical value was never seen at training time
- **THEN** its model input still encodes to the model's `UNKNOWN` category for that value, the
  same way offline prediction already handles it
