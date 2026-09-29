# Spec Delta

## MODIFIED Requirements

### Requirement: Training rows use only facts knowable before the train runs
Every feature of a training row SHALL be derivable from the timetable, calendar and station data
before the run takes place. No feature SHALL come from the run's own observed times, delays,
cancellations, platform changes or disruption messages, nor from any other run on the same
service date.

The feature set SHALL cover:
- station, train, line, category and operator
- weekday and holiday flag
- the service date's time of year, from its day and month only
- scheduled time at the stop, and the trip's start time
- the stop's position along its route
- station coordinates and federal state
- the direction of travel

The time-of-year feature SHALL NOT encode the year, and SHALL be cyclic: the last day of December
and the first day of January SHALL be as close as any two consecutive days.

#### Scenario: A live-only field is not a feature
- **WHEN** the training dataset is built
- **THEN** none of its features is taken from actual times, delays, cancellation flags, changed
  platforms or disruption messages of any run

#### Scenario: Training and prediction build features the same way
- **WHEN** a feature row is built for a historical stop during training, and for the same
  scheduled call during prediction
- **THEN** both rows contain identical feature values

#### Scenario: The same calendar day in different years
- **WHEN** feature rows are built for the same scheduled call on 2025-11-20 and on 2026-11-20
- **THEN** their time-of-year values are identical

#### Scenario: The year boundary is not a jump
- **WHEN** time-of-year values are computed for 31 December and 1 January
- **THEN** their distance equals the distance between any other two consecutive days, within
  rounding

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

## ADDED Requirements

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
