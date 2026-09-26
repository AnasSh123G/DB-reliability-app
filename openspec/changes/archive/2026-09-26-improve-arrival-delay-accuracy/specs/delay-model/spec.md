# Spec Delta

## MODIFIED Requirements

### Requirement: Only trustworthy observations become training targets
A stop SHALL become a training target only if its arrival observation is recorded (not a
forecast or missing), the stop is not cancelled, and the arrival delay is within the same
plausibility limit the stored averages use. Each target SHALL be that individual arrival's
recorded delay, never a mean or other aggregate across runs. New arrival-only candidates SHALL
NOT use departure delay as a training target. This SHALL NOT delete stored departure observations
or prevent evaluating the arrival output of a previously trained two-side model.

#### Scenario: Cancelled stops are excluded
- **WHEN** a stop is cancelled
- **THEN** it contributes no arrival target

#### Scenario: An implausible delay on one side does not discard the other
- **WHEN** a stop has a valid recorded arrival but an implausible departure delay
- **THEN** the arrival remains an eligible target; an implausible arrival is excluded regardless
  of the departure value

#### Scenario: Repeated services remain separate targets
- **WHEN** the same service at the same station arrives 1, 4 and 12 minutes late on three dates
- **THEN** the dataset retains three individual targets, not one average delay

### Requirement: Predicted percentiles are ordered
When a predictor provides percentiles, its 25th percentile SHALL NOT exceed its median, and its
median SHALL NOT exceed its 75th percentile. A candidate's point estimate SHALL be identified
separately if its decision rule is not the median. Point-only candidates SHALL NOT fabricate a
percentile band or label their point estimate as a median without that interpretation.

#### Scenario: Crossed quantile outputs are corrected
- **WHEN** a candidate's raw percentile outputs for a call are out of order
- **THEN** the returned 25th, 50th and 75th percentiles are in non-decreasing order

#### Scenario: A formula supplies only a point estimate
- **WHEN** a statistical candidate produces an arrival estimate without percentiles
- **THEN** the estimate is scored against the actual arrival and no percentile band is invented

### Requirement: A trained model is saved as a versioned artifact
Each training run SHALL write a new artifact directory that is never overwritten. The directory
SHALL contain:
- the chosen arrival model
- every evaluated candidate's individual prediction/outcome rows and summary scores
- the feature list and the category vocabularies
- the training, validation and test date ranges
- the forecast cutoff policy, point-estimate decision rule and data provenance
- the library versions used

A pointer SHALL identify which artifact the prediction command uses. Saving an experimental
artifact SHALL NOT automatically replace the deployed artifact.

#### Scenario: Retraining keeps the previous artifact
- **WHEN** training runs a second time
- **THEN** a new artifact directory is created, the previous one is untouched, and the pointer
  moves only after the new artifact is complete, deployment checks pass and replacement is approved

### Requirement: Delays can be predicted offline from the command line
After an arrival-only artifact is approved, the CLI SHALL predict, for a chosen train or line,
station, date and time, an arrival delay estimate, with a 25th–75th percentile band only when the
predictor supports it. It SHALL use only the saved prediction assets and timetable, calendar and
station data. It SHALL NOT read production stored averages or observed trip data at inference,
and it SHALL NOT predict departure delay with the new artifact. The previous artifact SHALL
remain usable until replacement.

#### Scenario: Prediction works without any averages
- **WHEN** the stored averages tables are empty and a user requests a prediction for a scheduled
  call
- **THEN** an arrival estimate is still printed when that call has a scheduled arrival, with a
  band only if supported by the predictor

#### Scenario: A call the model never saw in training still gets a prediction
- **WHEN** a user requests a prediction for a train and station whose combination has no
  training observations
- **THEN** a prediction is printed, based on the call's other features

#### Scenario: A call with no scheduled arrival has no prediction
- **WHEN** the requested call has no scheduled arrival (it is the trip's origin)
- **THEN** no arrival prediction is printed

#### Scenario: A terminating call has no departure prediction
- **WHEN** the requested call has no scheduled departure (it is the trip's terminus)
- **THEN** no departure prediction is printed — as with every other call, since departure is
  never predicted

#### Scenario: No trained model exists
- **WHEN** a user requests a prediction before any model has been trained
- **THEN** the command fails with a message saying which training command to run, and prints
  no figures

### Requirement: The current model artifact is servable to a browser client
The system SHALL make the prediction assets of the artifact `CURRENT` points to retrievable over
HTTP, with immutable versioned cache keys. Its manifest SHALL identify the runtime, input/output
contract and required assets, including an ONNX model or the approved formula's coefficients and
tables. Serving these assets SHALL NOT perform inference on behalf of the browser.

#### Scenario: Fetching the current artifact
- **WHEN** a client requests the current prediction assets
- **THEN** it receives the same serialized assets verified against the reference predictor,
  with an artifact cache key that changes only when `CURRENT` moves

#### Scenario: No trained model exists
- **WHEN** no artifact has ever been exported
- **THEN** the request fails clearly, and neither browser inference nor scheduled-call input
  encoding silently receives fabricated data

### Requirement: An interrupted run resumes from its last finished step
A run SHALL save each finished step to disk as soon as it is done: every tuning trial, and the
final model and predictions. A resumed run SHALL skip every saved step and continue with the
next one, with the tuning budget reduced by the time already spent. A saved step SHALL only be
reused for the same dataset, date split and search settings. Anything else SHALL be refused with
a message, not silently mixed in.

#### Scenario: A kill during a final fit loses only that fit
- **WHEN** a run is killed during a library's final fit after its tuning had finished
- **THEN** resuming starts that library's final fit directly, without tuning again

#### Scenario: A kill during tuning keeps the finished trials
- **WHEN** a run is killed during tuning after some trials had finished
- **THEN** resuming continues the same search with those trials counted, and spends only the
  rest of the budget

#### Scenario: Saved work from a different dataset is not reused
- **WHEN** a resume finds saved steps from a different dataset, split or search space
- **THEN** it stops with a message that names the mismatch, and trains nothing

## REMOVED Requirements

### Requirement: Candidate models are compared against baselines on the same test days
**Reason**: The change evaluates predictions of individual arrivals, not improvement over pooled
averages. It also selects candidate families after research rather than mandating three tree
libraries regardless of suitability.
**Migration**: Use the individual-arrival evaluation and research checkpoint below. Existing
averages tables and archived benchmark reports remain unchanged.

## ADDED Requirements

### Requirement: A forecast represents one future scheduled arrival
Each forecast SHALL identify the service date, run, station and scheduled arrival being predicted,
and the forecast cutoff. Only information available before that cutoff SHALL be used. For a
historical replay, prediction assets SHALL be fitted exclusively from permitted earlier history,
even if the replay itself is executed later; it SHALL be labeled a replay, not a forecast logged
before the event. The target run's observed delays and later information SHALL NOT be input features.
A five-hour-ahead evaluation SHALL use a cutoff five hours before scheduled arrival; fitting,
preprocessing and any embedded historical statistics SHALL use permitted earlier history only.

#### Scenario: An arrival is predicted five hours ahead
- **WHEN** a call is scheduled to arrive at 18:00 and its forecast cutoff is 13:00
- **THEN** information learned after 13:00 cannot affect the prediction, and the eventual recorded
  arrival supplies the outcome against which it is evaluated

#### Scenario: A historical replay lacks a contemporaneous timetable snapshot
- **WHEN** the evaluator cannot establish which timetable data was available at the cutoff
- **THEN** it discloses that limitation and does not describe the replay as a verified prospective
  five-hour-ahead forecast

### Requirement: Candidate accuracy is measured against individual recorded arrivals
Every selected candidate, including the incumbent's arrival output, SHALL be evaluated on the
same eligible held-out arrival instances. Each error SHALL be the absolute difference between
the candidate's point prediction and that instance's actual recorded delay, in minutes. No mean,
median or other aggregate across service dates SHALL replace the recorded outcome.

The primary score SHALL be the share of eligible arrivals with error at most 3 minutes. Reports
SHALL also give shares within 1 and 5 minutes, average absolute error over scored instances,
eligible/scored/missing counts and coverage. Missing predictions SHALL NOT count as successful
predictions or silently disappear from the headline denominator. The error average summarizes
individual errors; it is not a historical mean used as a target.

Reports SHALL retain per-instance forecast, outcome and error records; show results by service
date, train segment and holiday/normal day type; and disclose exclusion reasons. Valid exceptional
delays SHALL remain in headline scores. Separately reported subsets SHALL disclose their rules
and counts rather than imply that an excluded delay was known to be unpredictable beforehand.

#### Scenario: An individual prediction is within tolerance
- **WHEN** a candidate predicts 4 minutes late and that arrival is actually 6 minutes late
- **THEN** its error is 2 minutes: it counts within 3 and 5 minutes, but not within 1 minute

#### Scenario: A point lies exactly on the tolerance boundary
- **WHEN** the absolute error is exactly 3 minutes before presentation rounding
- **THEN** it counts as within 3 minutes

#### Scenario: A difficult call cannot be scored
- **WHEN** a candidate has no prediction for an otherwise eligible arrival
- **THEN** the report retains the arrival as missing and does not improve the headline hit rate
  by excluding it

#### Scenario: There is no stored average for a call
- **WHEN** an eligible arrival has no entry in the production averages tables
- **THEN** it remains in the evaluation; stored-average availability is not an inclusion rule

#### Scenario: Candidates are selected without looking at final test outcomes
- **WHEN** architectures, formulas, settings or point-estimate rules are compared
- **THEN** selection uses validation instances, primarily their within-3-minute score, and final
  test instances are used only for the locked evaluation, not retuning

### Requirement: Research precedes the choice of replacement predictor
Before candidate-specific implementation and expensive training, the change SHALL document and
review neural and statistical alternatives for individual advance arrival forecasts, including
their required data, training device support, browser inference feasibility and expected asset
size. Suitability claims SHALL distinguish verified evidence from hypotheses. The user SHALL
approve the candidate shortlist and bounded experiment plan before those experiments begin.
A hierarchical estimator SHALL NOT be mandatory or assumed superior.

#### Scenario: A method requires live upstream delays
- **WHEN** a researched method depends on inputs unavailable at the forecast cutoff
- **THEN** it is marked unsuitable for this advance-only experiment unless an explicit change
  to the information boundary is approved

#### Scenario: A method cannot fit the browser constraints
- **WHEN** an alternative requires oversized prediction assets or server-side inference
- **THEN** it is not selected as a deployable candidate without a demonstrated compliant variant

### Requirement: Replacement depends on per-arrival evidence and deployment approval
There SHALL be no required improvement margin against flat or hierarchical historical averages.
The report SHALL present candidate and incumbent errors against actual arrivals, including whether
within-3-minute accuracy improved. It SHALL NOT promise a minimum achievable accuracy. An
experimental artifact SHALL leave `CURRENT` unchanged until browser size, compatibility and
prediction-parity checks pass and the user approves deployment.

#### Scenario: Research yields no convincing improvement
- **WHEN** candidate results do not establish a worthwhile improvement in individual-arrival
  accuracy or the candidate fails deployment checks
- **THEN** the report records the outcome and the current artifact remains in use

#### Scenario: A replacement is approved after evaluation
- **WHEN** the user approves a complete, validated replacement after reviewing its results
- **THEN** the pointer moves atomically, preserving the previous artifact for rollback

### Requirement: The exported browser artifact fits a size budget
Prediction-specific assets required by the browser SHALL total no more than 35 MB
(35,000,000 uncompressed bytes), including model weights or formula coefficients/tables and
required metadata. The browser SHALL perform inference itself. Shared application/runtime assets
SHALL be reported separately. Export alone SHALL NOT move `CURRENT`.

#### Scenario: An oversized export is refused
- **WHEN** the combined prediction-specific assets exceed 35 MB
- **THEN** they are marked ineligible for deployment with their measured size and budget, and
  `CURRENT` remains unchanged

#### Scenario: A formula runs in the browser without a model server
- **WHEN** a compliant statistical predictor is selected
- **THEN** the browser computes its arrival estimate from its versioned assets and scheduled-call
  inputs without requesting server-side prediction
