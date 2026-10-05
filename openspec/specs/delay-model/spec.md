# Delay Model Specification

## Purpose

Trains, evaluates and queries a local model that predicts a scheduled call's arrival and
departure delay from the timetable alone, without reading any stored delay averages at
prediction time.

## Requirements

### Requirement: Training rows use only facts knowable before the train runs
Every feature of a training row SHALL be derivable from the timetable, calendar and station data
before the run takes place, or be a historical notice rate that meets the notice-rate requirement
below. No feature SHALL come from the run's own observed times, delays, cancellations, platform
changes or messages, nor from any other run on the same service date.

The feature set SHALL cover:
- station, train, line, category and operator
- weekday and holiday flag
- the service date's time of year, from its day and month only
- scheduled time at the stop, and the trip's start time
- the stop's position along its route
- station coordinates and federal state
- the direction of travel
- the historical notice rates of the station, line and train

The time-of-year feature SHALL NOT encode the year, and SHALL be cyclic: the last day of December
and the first day of January SHALL be as close as any two consecutive days.

#### Scenario: A live-only field is not a feature
- **WHEN** the training dataset is built
- **THEN** none of its features is taken from actual times, delays, cancellation flags, changed
  platforms or messages of the run itself or of any run on the same service date

#### Scenario: Training and prediction build features the same way
- **WHEN** a feature row is built for a historical stop during training, and for the same
  scheduled call during prediction
- **THEN** both rows contain identical feature values, given the same stored notice rates

#### Scenario: The same calendar day in different years
- **WHEN** feature rows are built for the same scheduled call on 2025-11-20 and on 2026-11-20
- **THEN** their time-of-year values are identical

#### Scenario: The year boundary is not a jump
- **WHEN** time-of-year values are computed for 31 December and 1 January
- **THEN** their distance equals the distance between any other two consecutive days, within
  rounding

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

### Requirement: Evaluation splits by whole service dates in time order
Training, validation and test sets SHALL be disjoint ranges of whole service dates, in that
chronological order. No service date SHALL appear in more than one set. The test dates SHALL be
used only for the final scoring, never for tuning or model selection.

The ranges SHALL be derived from the dataset's own complete dates by a documented rule, not from
date lists fixed in code: the test range is the latest complete dates not yet inspected, the
validation range the complete dates immediately before it, and training everything earlier.
The rule's window lengths SHALL be recorded with the run. Stub dates SHALL be excluded as before.

#### Scenario: No run is split across sets
- **WHEN** the dataset is split
- **THEN** every stop of a given run lies in the same set as every other stop of that run

#### Scenario: Tuning never sees test days
- **WHEN** hyperparameters are searched or a candidate is chosen
- **THEN** only training and validation dates are used

#### Scenario: New data moves the split forward
- **WHEN** the dataset is rebuilt after two more weeks of arrivals have been collected
- **THEN** the validation and test ranges move to the new latest dates without any code change,
  and the previous test dates become validation or training dates

#### Scenario: Too little data for the rule
- **WHEN** the dataset has fewer complete dates than the validation and test windows plus a
  minimum training range need
- **THEN** the split fails with a message naming the dates available and the dates required

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

### Requirement: Candidate accuracy is measured against individual recorded arrivals
Every selected candidate, including the incumbent's arrival output, SHALL be evaluated on the
same eligible held-out arrival instances. Each error SHALL be the absolute difference between
the candidate's point prediction and that instance's actual recorded delay, in minutes. No mean,
median or other aggregate across service dates SHALL replace the recorded outcome.

The primary score SHALL be the average absolute error over eligible arrivals. Reports SHALL also
give shares within 1, 3 and 5 minutes, eligible/scored/missing counts and coverage. Missing
predictions SHALL NOT count as successful predictions or silently disappear from the headline
denominator; a candidate with missing predictions SHALL NOT be ranked above one with full
coverage on average error alone. The error average summarizes individual errors; it is not a
historical mean used as a target.

Reports SHALL retain per-instance forecast, outcome and error records; show results by service
date, calendar month, train segment and holiday/normal day type; and disclose exclusion reasons.
Valid exceptional delays SHALL remain in headline scores. Separately reported subsets SHALL
disclose their rules and counts rather than imply that an excluded delay was known to be
unpredictable beforehand.

#### Scenario: An individual prediction is within tolerance
- **WHEN** a candidate predicts 4 minutes late and that arrival is actually 6 minutes late
- **THEN** its error is 2 minutes: it counts within 3 and 5 minutes, but not within 1 minute

#### Scenario: A point lies exactly on the tolerance boundary
- **WHEN** the absolute error is exactly 3 minutes before presentation rounding
- **THEN** it counts as within 3 minutes

#### Scenario: A difficult call cannot be scored
- **WHEN** a candidate has no prediction for an otherwise eligible arrival
- **THEN** the report retains the arrival as missing and does not improve the headline score
  by excluding it

#### Scenario: There is no stored average for a call
- **WHEN** an eligible arrival has no entry in the production averages tables
- **THEN** it remains in the evaluation; stored-average availability is not an inclusion rule

#### Scenario: Candidates are selected without looking at final test outcomes
- **WHEN** architectures, formulas, settings or training epochs are compared
- **THEN** selection uses validation instances, primarily their average absolute error, and final
  test instances are used only for the locked evaluation, not retuning

#### Scenario: Accuracy is shown per calendar month
- **WHEN** a report is written for validation or test arrivals spanning more than one month
- **THEN** it lists each calendar month's eligible count, average absolute error and within-1/3/5
  shares

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

### Requirement: Replacement depends on per-arrival evidence and deployment approval
There SHALL be no required improvement margin against flat or hierarchical historical averages.
The report SHALL present candidate and incumbent errors against actual arrivals on the same test
instances, including whether average absolute error improved, alongside the within-1/3/5-minute
shares. The two-sided TabM artifact `20260924T183944Z` SHALL be scored on those same instances in
every report, whether or not it is still `CURRENT`, and SHALL NOT be deleted. It SHALL NOT
promise a minimum achievable accuracy. An experimental artifact SHALL leave `CURRENT` unchanged
until browser size, compatibility and prediction-parity checks pass and the user approves
deployment.

#### Scenario: Research yields no convincing improvement
- **WHEN** candidate results do not establish a worthwhile improvement in individual-arrival
  accuracy or the candidate fails deployment checks
- **THEN** the report records the outcome and the current artifact remains in use

#### Scenario: A replacement is approved after evaluation
- **WHEN** the user approves a complete, validated replacement after reviewing its results
- **THEN** the pointer moves atomically, preserving the previous artifact for rollback

#### Scenario: The old TabM is compared after it stops being current
- **WHEN** a report is written after `CURRENT` has moved away from `20260924T183944Z`
- **THEN** the report still lists that TabM's arrival errors on the same instances as every
  candidate

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

### Requirement: Training runs are isolated from the desktop and capped in memory
A bake-off or model search SHALL be started as its own background service. The service SHALL
NOT belong to the process tree or memory accounting group of the editor, the terminal, or the
session that launched it. It SHALL run under a hard memory cap and SHALL NOT use swap. Reaching
the cap SHALL stop only that service. After the service ends, its exit reason (finished, failed
or memory limit) SHALL still be readable.

#### Scenario: Closing the editor does not stop training
- **WHEN** a run was launched and the editor, terminal or launching session is then closed or
  killed
- **THEN** the run keeps going

#### Scenario: A run that exceeds its memory cap is stopped alone
- **WHEN** a run's memory reaches the cap
- **THEN** only that run is stopped, no other process is killed, and its exit reason reports the
  memory limit

#### Scenario: A finished run can still be inspected
- **WHEN** a run has ended, successfully or not
- **THEN** its exit reason and its log can still be read, and starting the next run does not
  overwrite the previous run's log

### Requirement: Reported memory includes swapped memory
Every memory figure a training run logs SHALL include the run's swapped-out memory as well as
its resident memory. The run SHALL also log the peak of its memory group.

#### Scenario: Memory pushed to swap is not reported as freed
- **WHEN** part of a run's memory is moved to swap
- **THEN** the logged total does not fall, and the swapped share is shown

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

### Requirement: A memory check precedes a full run
Before a full run is launched, a short capped probe SHALL measure each selected library's peak
memory at the tuning sample size and at a fraction of the final-fit size. It SHALL then project
the peak at the full final-fit size. The launcher SHALL refuse to start a full run whose projected
peak exceeds its memory cap, unless explicitly overridden.

#### Scenario: A run that would not fit is not started
- **WHEN** the probe projects a library's final-fit peak above the memory cap
- **THEN** the full run is not launched, and the message names the library, its projected
  peak and the cap
</content>

### Requirement: A large comparison model is trained alongside the deployable one
Each full training run SHALL also produce a large comparison model. It SHALL use the same
dataset, date split, features, vocabulary rules, point estimate and selection score as the
deployable candidates, with larger networks allowed. Its prediction assets SHALL total no more
than 100 MB (100,000,000 uncompressed bytes). It SHALL be saved as a versioned artifact marked as
comparison-only, in a location the web server does not serve from. It SHALL be scored on the same
validation and test instances as the deployable model and appear in the same reports. The CLI
SHALL be able to predict with it when asked for that artifact explicitly.

A comparison-only artifact SHALL NOT become `CURRENT` and SHALL NOT be served to the browser.

#### Scenario: The large model appears in the report
- **WHEN** a full training run finishes
- **THEN** its report lists the large model's scores, per-month breakdown and asset size next to
  the deployable model's and the references', on identical test instances

#### Scenario: Promoting the large model is refused
- **WHEN** someone tries to point `CURRENT` at a comparison-only artifact
- **THEN** the pointer does not move and the command names the artifact as comparison-only

#### Scenario: An oversized comparison export is refused
- **WHEN** the large model's prediction assets exceed 100 MB
- **THEN** no comparison artifact is written, and the measured size and budget are reported

#### Scenario: Predicting with the large model from the CLI
- **WHEN** a user runs the prediction command and names the comparison artifact
- **THEN** it prints that model's arrival median and band for the call, and `CURRENT` is not read
  or changed

### Requirement: The shown arrival estimate is the predicted median
A deployable arrival artifact SHALL provide a median arrival delay as its point estimate, with a
25th and 75th percentile. The browser, CLI and live accuracy study SHALL show that median as the
model's estimate and label it as a median. A point rule other than the median (such as the centre
of the most likely fixed-width window) SHALL NOT be used for a deployable artifact.

#### Scenario: A model with a binned distribution
- **WHEN** a candidate outputs a probability per delay bin
- **THEN** its point estimate is the median of that distribution, not the centre of its densest
  window

#### Scenario: Estimates are not a near-constant
- **WHEN** a candidate's validation point estimates are summarised
- **THEN** the report states the share taken by its single most frequent value, so a
  near-constant predictor is visible before approval

### Requirement: The training dataset covers the full collected history
The arrival dataset SHALL be buildable from every eligible recorded arrival in the database, for
at least 12 months of national data, and a model search SHALL be able to train and validate on
it, each without exceeding the memory cap of its launch job. Reading the dataset for training
SHALL NOT require holding every column of every row in memory at once.

#### Scenario: A year of national data fits the cap
- **WHEN** the dataset is built from 12 months of national arrivals and a search is launched on
  it under its memory cap
- **THEN** both finish without reaching the cap, and their logged peaks are below it

#### Scenario: The existing dataset is not overwritten
- **WHEN** the full-history dataset is built
- **THEN** it is written alongside, not over, the existing arrival dataset and deployed model's
  training data

### Requirement: No GPU tensor reaches 2 GiB
Training and prediction on the RX 9070 XT SHALL keep every GPU tensor below 2^31 bytes,
including copies of input data, targets and batch activations. A configuration that would create
a larger tensor SHALL be refused before training starts.

#### Scenario: A training set larger than 2 GiB
- **WHEN** the training rows' encoded inputs occupy more than 2 GiB in total
- **THEN** they are sent to the GPU in batches each below 2 GiB, and training proceeds

#### Scenario: An oversized batch is refused
- **WHEN** a configuration's batch size would make any batch tensor reach 2 GiB
- **THEN** the run stops before training with a message naming the tensor and its size

### Requirement: The train vocabulary stays bounded
A train SHALL receive its own learned category only if the training history contains at least a
configured minimum number of its arrivals, of which at least one lies within a configured period
before the end of training history. Every other train SHALL encode as `UNKNOWN`, both in training
and prediction. Both thresholds SHALL be recorded in the artifact.

#### Scenario: A train that stopped running months ago
- **WHEN** a train has many training arrivals but none within the recency period
- **THEN** it has no category of its own and its calls are predicted as `UNKNOWN` train

#### Scenario: A rarely seen train
- **WHEN** a train has fewer training arrivals than the minimum
- **THEN** it encodes as `UNKNOWN`, and its calls still receive a prediction from their other
  features

### Requirement: Notice rates describe how often a place or train has notices
The system SHALL offer six numeric inputs per arrival: for each of the station, the line and the
train, the share of past arrivals whose trip had a construction notice and the share whose trip
had a disruption notice. A construction notice is a stored message whose category starts with
"Bauarbeiten"; a disruption notice one whose category starts with "Störung" or "Großstörung".
Only arrivals eligible as training targets SHALL be counted.

A rate SHALL be smoothed toward the rate one level up (train toward line, line toward station,
station toward all stations) with a recorded weight, so that a key with few arrivals stays close
to its parent. A key with no arrivals at all SHALL give a missing value, never zero, and the
model's input layout SHALL include the missing indicator for it. The line level SHALL be missing
for a train without a line.

#### Scenario: A busy station's rate is its share of arrivals
- **WHEN** a station has 10,000 counted arrivals, 1,200 of them on trips with a disruption notice
- **THEN** its disruption rate is close to 0.12, differing from it only by the smoothing toward
  the all-station rate

#### Scenario: A rarely seen train stays near its line
- **WHEN** a train has 3 counted arrivals, all on trips with a construction notice, and its line's
  construction rate is 0.10
- **THEN** the train's construction rate is far below 1.0 and closer to 0.10 than to 1.0

#### Scenario: A train with no history is missing, not zero
- **WHEN** a train has no counted arrivals in the dates the rates come from
- **THEN** its train-level rates are missing and the input's missing indicator is 1

### Requirement: Notice rates never come from the row's own period
A training row's notice rates SHALL be computed from the training dates outside the calendar
month of the row's service date. Rates for validation rows, test rows and prediction SHALL be
computed from the training dates only. No date after the end of the training range SHALL
contribute to any rate used by a model trained on that range.

#### Scenario: A training row does not count its own month
- **WHEN** the rates of a training row on 2026-03-14 are computed
- **THEN** no arrival from March 2026 contributes to them

#### Scenario: Validation and test days stay out
- **WHEN** the rates for a validation or test row, or for a live prediction, are computed
- **THEN** only arrivals on training dates contribute

#### Scenario: A notice caused by a delay cannot reach its own arrival
- **WHEN** a trip has a disruption notice because its train was already late
- **THEN** that notice is not part of any rate the same trip's arrivals are trained or scored with

### Requirement: Prediction uses stored notice rates only
Predictions SHALL read notice rates from stored data and SHALL NOT call any external service or
read live notices. The artifact SHALL record which training dates its rates come from and the
smoothing weight, and a model that uses the rates SHALL refuse to predict when the stored data
no longer matches that record. The rates SHALL NOT be part of the browser download. Artifacts
without the rates in their input list SHALL predict as before.

#### Scenario: No external call
- **WHEN** a forecast is requested for a trip a week from now
- **THEN** its notice-rate inputs are read from stored data and no network request is made

#### Scenario: Changed rate data is refused
- **WHEN** the stored notice counts for the artifact's recorded dates have changed since training
- **THEN** prediction with that artifact fails with a message naming the mismatch, rather than
  using different rates than the model was trained with

#### Scenario: An older artifact is unaffected
- **WHEN** an artifact trained before this change is used
- **THEN** its input is built from its own recorded input list and the notice rates are not read

### Requirement: The search reports what the notice rates add
The compact track SHALL include an ablation trial that repeats the best network without the
notice-rate inputs, in addition to the existing time-of-year ablation. The ranking and the final
report SHALL show both ablations' validation average error next to the winner's.

#### Scenario: The ablation is ranked on the same rows
- **WHEN** the compact track finishes
- **THEN** the ranking lists the no-notice-rates trial with its validation average error on the
  same arrivals as every other trial
