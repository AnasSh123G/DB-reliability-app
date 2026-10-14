## ADDED Requirements

### Requirement: Model variants are compared with a matched control
A training-variant experiment SHALL train a control and each variant on identical corrected data,
architecture, epoch budget and evaluation rows, differing only in the variant's single change
(training-date weighting or the time-of-year input). The report SHALL compare each variant with
the control on the same arrivals, within 1/3/5 minutes and by average error, with paired per-day
intervals.

#### Scenario: A variant changes only one thing
- **WHEN** the `strong` or `seasonal` condition is trained
- **THEN** its key differs from control only in the recency amplitude, or only in the input trial
  and its time-of-year columns

#### Scenario: Time of year has no year in it
- **WHEN** the seasonal inputs are computed for a row
- **THEN** they come from the service date's month and day alone, and two rows with the same
  month and day in different years get identical values
