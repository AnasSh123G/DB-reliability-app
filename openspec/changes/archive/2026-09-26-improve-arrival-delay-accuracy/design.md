# Design

## Context

See `proposal.md` for the goal. The research audit below is complete; candidate
implementation, new experiments and deployment have not started.

Observed project facts:

- The deployed TabM file is `data/models/v2/20260924T183944Z/model.onnx`, 20,480,648 bytes.
  It has arrival and departure percentile outputs and runs through browser ONNX Runtime WASM.
  `src/dbrel/ml/nn.py` and `bahn-buddy-predict/src/lib/tabm.ts` assume that output layout.
- `scripts/experiment_nn.py` already has TabM and residual-MLP paths. The recorded TabM run used
  20 training dates, 4 validation dates and 6 test dates, not several years of seasonal history.
  It stopped after six epochs, with its best validation loss around epoch three.
- `features.py` supplies identity, calendar, scheduled times, route position, direction and
  location. Explicit destination identity and arrival-specific scheduled-time features deserve
  investigation; the current shared scheduled-time input uses departure when available.
- `metrics.py` compares each predicted median to each actual recorded delay already. The old
  benchmark did NOT use historical averages as ground truth. However, it selects using quantile
  loss and does not prioritize the user's within-three-minute goal.
- `baselines.py` uses historical group quantiles. Production `derive/stop_averages.py` stores
  arithmetic means pooled across matching dates, with a holiday/normal day-type key. Neither
  groups by a specific evaluation date. An exact group with one observation is covered; zero
  matching observations means uncovered. These tables are not the new evaluation target.
- The archived offline-model design records a particular six-model LightGBM export at about
  3 GB, and says most TabM weight storage is its train embedding. This does not prove every
  possible compact tree model is oversized, nor that removing one output head halves TabM cost.
- `data/build/torch-rocm/venv` and CPU/GPU parity-check machinery already exist. The saved TabM
  experiment records a passed parity check. Reuse and inspect that environment before changing
  it; this plan does not establish that a new source build or ROCm upgrade is needed.
- `src/dbrel/study/live_accuracy.py` already retains individual actual delays and scores misses
  within 1/3/5 minutes. Its collection/matching rules are documented in
  `.claude/skills/live-accuracy-study/SKILL.md`. No new live run is authorized by this revision.

## Goals / Non-Goals

**Goals:**
- Improve prediction of one future arrival, not reproduction of a historical group mean.
- Test compact neural models and statistical formulas on the same individual outcomes.
- Use within-three-minute accuracy as the primary selection criterion, with visible misses,
  coverage and per-day/segment results, not a fabricated guarantee for one future train.
- Verify browser feasibility and local GPU support before spending a full training budget.

**Non-Goals:**
- Changing production averages, removing departure observations, or using averages as labels.
- Requiring a new model to beat an average by a fixed number of minutes.
- Predicting accidents, adding live upstream-delay features, or silently filtering hard cases.
- Picking an architecture from generic leaderboard claims without checking this data and runtime.
- Training, installing GPU packages or changing the deployed model during this planning revision.

## Decisions

### D1. The unit of prediction is one scheduled arrival

Identify an instance by service date, run, station and scheduled arrival (including route position
where repeated station visits require disambiguation). Retain its actual delay separately from
its features. A forecast at 13:00 for an 18:00 arrival predicts that occurrence, not all occurrences
of the service. A four-minute prediction and six-minute actual delay have a two-minute error.

Training can learn patterns across historical instances; a statistical formula can estimate a
conditional distribution from history. Neither turns the evaluation outcome into an average.
Point estimates derived from distributions remain forecasts of the individual instance.

### D2. Evaluate what could have been known before the forecast

Reuse `split.py`'s whole-service-date chronological separation. Fit parameters, vocabularies,
normalization and any historical summaries on training history only. Select settings on
validation instances and lock them before the final test. In a five-hour replay, fitting history
must also predate each cutoff; calls just after midnight may require excluding recent training
data or moving the evaluation start. Merely being outside the test dates is not enough.

Use no target-run observations or same-service-date observations as features. Calendar and
timetable features must respect their availability at the cutoff. Historical replay with a
present-day timetable is not proof of what was available five hours earlier; document that
limitation rather than claiming a prospective result.

The September 17–22 test scores have already been inspected in earlier experiments. They can
support development comparisons, but must not be represented as a fresh untouched test.
Reserve later unexamined dates for final scoring, or explicitly wait for new data.

The existing live study computes forecasts after a stop has occurred. It can validate individual
misses under a frozen, earlier artifact and verified input availability, but its archived API
change times are not independent proof of a five-hour-ahead logged forecast. Follow the study's
existing rules and disclose label/timetable limitations. Do not launch a new study without an
explicit run request.

### D3. Match selection to the three-minute tolerance

For every eligible instance, retain prediction, actual delay, absolute error and success flags
at 1/3/5 minutes before presentation rounding. Primary score: successful within-three-minute
predictions divided by all eligible instances. Report prediction coverage and scored-only error
statistics alongside it so abstentions cannot make the headline score look better.

Use average absolute error as a secondary summary/tie-breaker, not as a historical mean target.
Compare incumbent and candidate on the same instances. Show date, segment, holiday/normal type,
unknown-identity and support-count breakdowns where available. Do not require a stored average
to include an instance. Keep genuine large delays in the headline denominator; any diagnostic
subset must be named and must not claim to identify accidents from delay magnitude alone.

The current median minimizes expected absolute error, not necessarily the probability of being
within three minutes. Research a distribution-based decision rule choosing the estimate whose
three-minute neighborhood has the greatest predicted probability, alongside median regression.
That estimate need not be the median; exported metadata and labels must not call it p50.
If intervals are provided, preserve ordered, genuine percentiles. Do not synthesize fake bands
for point-only methods just to fit the existing three-output interface.

### D4. Research alternatives before selecting implementation

These are research tracks, NOT assertions of superiority or instructions to train all of them:

| Track | Why investigate it here | Questions to resolve before selection |
|---|---|---|
| Arrival-only TabM settings | Reuses a working compact GPU/browser path | Does arrival-only training help? Test embedding width, regularization, early stopping and ensemble size; measure rather than assume cost savings |
| Compact residual MLP / RealMLP-style training | Existing MLP implementation offers a practical alternative to TabM | Which training techniques transfer to high-cardinality train/station inputs and the tolerance objective? Verify actual export operators |
| Small FT-Transformer or another justified tabular network | Can model feature interactions differently | Is any gain worth browser activation memory and latency? Do not assume ONNX/Web compatibility |
| Conditional delay distribution with support-based smoothing | Can use time, day type, route and identity without a neural network | Can compact probability tables or parametric distributions improve individual misses rather than merely return another group mean? How are sparse/unknown calls handled? |
| Generalized additive / interaction models | Time-of-day curves and explicit station/route effects can be compact and inspectable | Which interactions are necessary? Can coefficients or lookup tables fit the total browser budget? |
| Timetable-based stochastic propagation | Historical running/dwell variation and scheduled slack may explain recurring accumulation/recovery | Are route linkage and running-time data trustworthy? Can the method forecast without live upstream delays or unavailable track/block dependencies? |
| Size-constrained trees or distilled students | The previous large LightGBM artifact does not rule out smaller variants | Demonstrate size/categorical encoding/runtime feasibility before further fitting; avoid repeating the oversized run |

Use primary papers, official repositories and runtime documentation in the research report;
record source links, versions and unresolved limitations. Inspect data availability and the
existing tests (`test_ml_features.py`, `test_ml_training.py`, `test_ml_nn.py`,
`test_ml_bakeoff.py`, `test_live_accuracy.py`) to make candidate-specific tasks concrete.
An exported ONNX file alone does not demonstrate that ONNX Runtime Web supports its operators.

A hierarchical/backoff estimator is a possible statistical candidate, not a mandatory baseline.
If used, keep day type at each fallback level; investigate whether dropping time/route detail
would erase the very recurring patterns being sought. Such estimators still learn from data:
"non-neural" does not mean assumption-free or automatically more accurate.

**Required checkpoint:** present a small, justified shortlist, exact targets/features/losses,
runtime/output contract, hardware recipe, trial budget and validation/test policy for approval.
Update candidate-specific tasks at that point, before implementation or expensive training.
If a formula or model requires additional data or consumer changes, surface the scope then.
Research-first selection is intentional; an unspecified final architecture is not permission to
silently substitute whichever implementation is easiest.

### D5. Verify the local AMD environment before any new neural training

Inspect Linux distribution/kernel, GPU architecture, driver, Python and torch/ROCm versions,
then check AMD's current Radeon/PyTorch compatibility documentation for that exact combination.
Prefer the existing proven environment. Consider official compatible wheels or containers only
if necessary; source compilation is a last resort after a demonstrated failure and approved
resource estimate, not a mandatory task. Do not install packages into system Python.

After the shortlist is approved, run a capped, small forward/backward parity probe of the selected
architecture. A successful device-name query is not a correctness or full-training test. Measure
host memory, VRAM and per-epoch time before sizing the experiment. Use the existing isolated
`dbrel ml launch` workflow for approved jobs, with memory limits and resumable checkpoints.
CPU is a viable option for cheap statistical methods; GPU support is library-specific.

### D6. Separate experiment artifacts from deployment

Preserve v2 artifacts and dataset caches. New arrival-only adapters must not overwrite the
existing two-side contract or break the incumbent reference. Train only on arrival targets for
new candidates; output shape depends on the approved method, not on a blanket six-to-three edit
across all libraries (tree candidates already model one side at a time).

Before full fitting, verify an approved candidate's inference path with a small export. Count
all required weights, coefficients, tables and metadata toward 35,000,000 uncompressed bytes.
Report shared WASM/application runtime downloads separately. Compression alone does not make an
oversized in-memory predictor satisfy the budget.

Evaluate the final exported predictor, not only Python training weights; test unknown categories,
midnight calls, origin/terminus handling and point/interval semantics. Verify Python/browser
agreement within a recorded numerical tolerance and measure browser load/inference latency.
Save experimental assets and reports without updating `CURRENT`.

There is no averages-based promotion margin. Present incumbent and candidate individual-arrival
results for user review. Preserve the current model when results are inconclusive. Switch only
after explicit deployment approval and complete compatibility checks.

## Risks / Trade-offs

- **[Limited history]** Millions of stop rows are not millions of independent operating days.
  → Report date coverage, keep all stops of a run together and avoid seasonal claims unsupported
  by the roughly month-long existing dataset.
- **[Irreducible uncertainty]** The timetable cannot reveal future incidents.
  → State uncertainty and observed hit rates; never guarantee that one five-hour forecast will
  be within three minutes.
- **[Headline dominance]** Frequent, near-on-time services can dominate aggregate accuracy.
  → Show segment/date and delay-range diagnostics without removing difficult arrivals.
- **[Objective mismatch]** A quantile-loss winner need not maximize three-minute hits.
  → Select on the specified validation metric and lock decision rules before the final test.
- **[Hardware/runtime assumptions]** A library can train on one backend yet fail in the browser.
  → Verify local parity and early browser export before a long run.
- **[Compatibility]** Point-only or arrival-only outputs affect existing consumers.
  → Resolve the schema and study compatibility at the checkpoint; do not deploy a known break.

## Migration Plan

1. Research sources, data sufficiency, local hardware and candidate deployment paths read-only.
2. Obtain approval for the shortlist, concrete implementation tasks and experiment resource caps.
3. Implement individual-arrival evaluation and approved candidate adapters while preserving v2.
4. Run approved probes and validation experiments; lock selection before a fresh final holdout.
5. Present per-instance evidence and exported-browser verification, then request deployment approval.
6. If approved, switch the versioned artifact atomically; rollback by restoring the previous
   pointer and compatible consumer path, never by deleting old assets or source data.

## Research Checkpoint Decisions

These are required decisions before candidate-specific implementation, not deferred numeric
defaults to guess during a full run:

- Which compact neural and statistical alternatives have sufficient evidence to test?
- Which scheduled-time/route features and historical information are demonstrably available
  at a five-hour cutoff?
- Which training loss and point-estimate rule target the user's tolerance without hiding errors?
- Which exact environment, runtime, output contract, fresh holdout and trial/resource budget
  will be used?

The previous open question about a margin over averages is removed. A smoothing strength is
only relevant if a smoothing-based candidate is selected, and is then tuned on validation, not test.

## Research findings: input and label audit (task 1.1)

| Question | Evidence in this repository | Consequence for a five-hour forecast |
|---|---|---|
| Unit and labels | `dataset.py` queries one `trip_stop` per recorded stop; `TripStop` has a unique `(trip_id, stop_sequence)`, `trip.service_date`, `supposed_arrival_utc` and `arr_delay_s`. The cache has 12,079,413 rows, 11,127,882 non-null arrival targets across 32 dates, 2026-08-21 through 2026-09-22. | Keep run/stop identity and observed arrival seconds alongside each prediction. The current NPZ saves `trip_id`, `day` and `arr_min` but **not** stop sequence, scheduled arrival UTC or station-stop ID; a new experiment view must preserve these without overwriting v2. |
| Eligibility | `dataset.py` requires `quality='RECORDED'`, no cancellation, known `is_holiday`, scheduled departure **or** arrival, and at least one plausible side. It masks arrival delays beyond 7,200 seconds separately; origin stops with departure only remain rows with NaN arrival. | Filter for eligible recorded arrival *per side*, not for presence of departure or a stored average; report exclusions. The 2-hour limit is a data plausibility rule, **not** an accident detector. |
| Scheduled time | Dataset feature `sched_local_sec` and `ScheduledCall.raw()` take `COALESCE(departure, arrival)`: for intermediate stops this is departure, not arrival. `StationStop.planned_arrival_sec` and `TripStop.supposed_arrival_utc` exist separately. | An arrival-only experiment can test arrival-specific time, but must produce matching historical and inference transformations; avoid using the old shared feature as if it were arrival time. |
| Route/destination | `Train.dest_name` and `route_path` are populated for recurring patterns, `dest_station_id` is null for all 73,375 local trains. `station_stop` has route-variant `path_sha1` and sequence; `timetable.calls_at()` chooses a variant and may return multiple visits for loops. | Destination can be derived from route/variant names, not assumed to have a numeric station ID. Resolve actual run variant and loop stop, preserving unknowns when unresolved. |
| Calendar/day type | `features.py` uses service-date weekday and the per-station holiday flag; `TripStop.is_holiday` treats Sundays/public holidays together, not a unique calendar date. | Day type can be a conditioning feature; observations of the same call on different dates remain distinct targets. |
| Date isolation | `split.py` separates whole service dates and remaps training-only vocab. Database has one feed version and trips through 2026-09-23, but that final day has only 166 trips versus ~44k on a full day. | The 2026-09-23 stub cannot serve as a fresh final holdout; September 17–22 scores were already examined. Obtain later untouched days or disclose that no fresh final evaluation is yet available. |
| What existed at 13:00? | `feed_version` has download/import timestamps but the local database has only one feed version and no recorded snapshots of each call's timetable as of each historical forecast cutoff. `train` patterns and `station_stop` can change through backfill; the live study computes predictions after events. | A retrospective calculation using the current timetable is a *historical replay*, not verified prospective five-hour performance. A genuine five-hour study requires storing dated forecasts or independently reconstructing pre-cutoff timetables. Do not run the API study or claim such a result without explicit scope and authorization. |

## Research findings: compact model families (task 1.2)

All sizes below are **estimates, not measured candidate exports**. Count every downloaded
prediction-specific asset (model, vocabularies, lookup tables) toward 35 MB, and measure actual
serialized bytes, browser WASM loading and single-call latency before accepting a candidate.
The existing two-sided TabM ONNX is 20,480,648 bytes (20.48 MB decimal), while the previous
LightGBM pickle is 3,142,269,887 bytes; an arrival-only output layer does **not** imply that
the TabM's shared embedding/trunk weights halve in size.

| Family and primary source | Objective and data needs | Budget and inference assessment | Evidence versus unverified claims |
|---|---|---|---|
| Arrival-only TabM, smaller widths/embeddings and regularization; [official TabM implementation](https://github.com/yandex-research/tabm) and [paper](https://arxiv.org/abs/2410.24210) | One eligible arrival per historical call; tune shared categorical embeddings, member count, width and early stopping against individual arrival targets. Try point-focused validation as well as existing quantile/pinball training. | Reuse existing PyTorch-to-ONNX and browser WASM path; current artifact is 20.48 MB and uses 512 hidden units and 16 members. A smaller version could fit, but measure export, vocabulary and per-call latency. | TabM's shared-weights ensemble and regression outputs are documented and the existing export works locally. **Unverified:** accuracy gain, how much removing departure saves, and browser behavior for any changed graph. |
| Small RealMLP-style residual or dense network; [standalone RealMLP-TD-S](https://github.com/dholzmueller/realmlp-td-s_standalone) and [paper](https://arxiv.org/abs/2407.04491) | Same arrival-only rows, trained-only robust scaling and missing/unknown categorical handling; train direct point/short-window or quantile output and select on held-out individual arrivals. | Roughly 0.3–2 million float32 weights for modest width/embedding configurations (~1.2–8 MB weights), plus ONNX overhead and vocabulary; simpler matrix ops than a transformer. **Estimate only**, architecture-specific. | Authors provide pre-tuned preprocessing and an MLP; the source does **not** establish a working browser export for this application. Implement preprocessing parity and export + browser tests first. |
| Small feature-token transformer; [FT-Transformer implementation/paper](https://github.com/yandex-research/rtdl-revisiting-models) | Same rows with numeric/categorical feature tokenization; tune a *bounded* depth/width and arrival-specific target; prevent target-encoded or future observations. | 1–5 million float32 weights (~4–20 MB weights) is a possible *design budget*, not a published size for this data. Attention may make single-call browser inference slower. | Official authors report a competitive tabular model but slower inference than MLPs. ONNX export and exact WASM ops/latency/size for a chosen graph **remain unverified**; do not assume WebGPU support. |
| Size-constrained shallow boosted trees, or distill tree predictions into an MLP; [ONNXMLTools converters](https://github.com/onnx/onnxmltools), [CatBoost ONNX notes](https://catboost.ai/docs/en/concepts/apply-onnx-ml) | Train arrival-only shallow/low-round boosted regressor on historical features, possibly use it as teacher for a small model trained on actual labels plus soft predictions. A teacher cannot use unavailable five-hour inputs. | The earlier ~3.14 GB LightGBM pickle rules out that *configuration*, not all small trees. Single-tree/ensemble ONNX-ML and any distillation artifact must be measured under 35 MB; compact MLP student is a browser alternative. | Converter project lists LightGBM/XGBoost/CatBoost; CatBoost ONNX exports only numeric features, so its native categorical path is not portable unchanged. No converter guarantees that the exported ensemble fits, runs in the browser, or outperforms TabM. |

[ONNX Runtime Web](https://onnxruntime.ai/docs/tutorials/web/) documents browser WASM
inference and a narrower WebGPU operator set; [its current WebGPU operator
table](https://github.com/microsoft/onnxruntime/blob/main/js/web/docs/webgpu-operators.md)
is **not** a guarantee of a candidate's complete browser execution, particularly for
`ai.onnx.ml` tree operators. Prefer the application's existing WASM path, and confirm each
export and numerical parity in a real browser. The transformer's relative merit and any
accuracy benefit of a bigger architecture are empirical, not established by another dataset's
benchmark.

## Research findings: statistical forecasters (task 1.3)

The evaluation target is a single run's observed arrival delay, not a group's mean. For a
within-three-minute *point* metric, the Bayes-optimal action for an accurately estimated
conditional distribution is the center of a six-minute interval containing the most
probability mass. The conditional mean minimizes squared loss, the conditional median
minimizes absolute loss; neither is guaranteed to maximize this hit rate. This follows from
the [conditional expected-loss decision rule](https://stephens999.github.io/fiveMinuteStats/decision_theory_bayes_rule.html),
not from a railway-specific empirical result. Fit histogram/window width and smoothing on
training data, choose the point rule on validation only, and disclose the tie convention and
whether a narrow peak simply sacrifices large-error performance. Three quantiles alone do
not specify the highest-mass six-minute window.

| Candidate formula | Single-call forecast and assumption | Missing-data and five-hour availability |
|---|---|---|
| Conditional delay histogram | Training-only empirical arrival-delay distribution for matching station, route/train, service-day type and scheduled time; choose the densest six-minute window or the median. Assumes enough recurrent calls and a reasonably stable distribution. | No matching group: explicit uncovered result or **specified** training-only fallback to a broader condition, with separate coverage. Bin tables/keys count toward 35 MB. Live train states not needed. The existing flat-average key pools the same type of service days across dates; don't mistake it for a date-specific average. |
| Additive/interaction correction | Start with a training-only global or route/time/day-type prior, add shrinkage-weighted station, train, and route-position residual effects; validation-tune penalty/interaction strength. Equivalently learn a small regularized lookup/log-linear conditional distribution and take the optimal window. Assumes recurrent effects persist over time and grouped noise is shrinkable. | Unknown train/combination falls back through an explicitly defined hierarchy; report its frequency and group support. Inputs can be precomputed from static schedules, but future-informed category encoding and evaluated-day group statistics are prohibited. [Partial pooling background](https://www2.stat.duke.edu/~pdh10/Teaching/732/Notes/shrinkage.pdf). |
| Scheduled slack / stochastic propagation | Estimate leg travel and dwell delay increments from earlier training runs and propagate a distribution along the **same** planned run/route; apply scheduled slack and a cap on recoverable time. Gives an individual call distribution conditional on route position. | A fully live propagation model conditions on the train's **observed preceding-stop delay** and/or other trains' states; these may not exist five hours beforehand, and historical DB labels are often finalized after the cutoff. Static-only propagation must marginalize unknown initial state and cannot secretly use actual earlier stops on the evaluated run. Route variants, connection identities and dwell/slack estimates require data validation; missing links need an explicit fallback. [Railway stochastic state/simulation example](https://arxiv.org/html/2512.19737). |

An unobserved incident cannot be forecast reliably from only schedule and history. The
[empirical railway propagation study](https://pmc.ncbi.nlm.nih.gov/articles/PMC7596077/)
documents network interactions, not guaranteed accuracy for this app; its disrupted regime
is outside the desired recurrent-delay focus. None of these formulas is automatically
superior to a trained model: compare on identical dated arrivals and report within-3,
MAE, coverage and stratified tail errors. Distinguish historical-only five-hour forecasts
from *live-updated* forecasts rather than mixing their scores.

## Research findings: local RX 9070 XT training path (task 1.4)

Read-only inspection found a Navi 48 RX 9070 XT (`0x7550`, `gfx1201`, 15.92 GiB)
on **CachyOS Linux**, kernel `7.2.6-1-cachyos`. `hipconfig --version` reports
`7.2.53211-3d9ef42` (7.2 series); the existing isolated
`data/build/torch-rocm/venv/bin/python` is Python 3.13.9 with PyTorch
`2.10.0+rocm7.0` (torch HIP `7.0.51831`) and ONNX 1.23.0. Torch sees the discrete
card as `cuda:0`, `gcnArchName=gfx1201`. **The system ROCm and wheel HIP versions
differ**; functional local tests do not establish that this mixed-version,
rolling-distribution combination is vendor-validated.

The saved `data/ml/experiments/nn_tabm.json` records a passing CPU/GPU loss and
gradient parity check (relative gradient difference ~1.38e-6), and a 442.4-second
training run. The exported model's manifest records ONNX-to-PyTorch maximum
absolute difference ~7.63e-6 on 20,000 rows; this is local export parity,
not a claim of prospectively verified browser accuracy. The separate ROCm
LightGBM experiment notes categorical-feature failures on this GPU; avoid
assuming that its GPU path is suitable for new candidates.

[AMD's versioned ROCm 7.2.3 compatibility matrix](https://rocm.docs.amd.com/en/docs-7.2.3/compatibility/compatibility-matrix.html)
lists `gfx1201` and PyTorch 2.9.1/2.8.0/2.7.1 on Ubuntu 24.04.4 and other
listed distributions, **not CachyOS** or this exact torch 2.10/ROCm 7.0 wheel
combination. [AMD's current PyTorch installation guide](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/frameworks/pytorch/install.html)
lists the RX 9070 XT and Python 3.13 on supported platforms but must not
be retroactively interpreted as certification of this local stack.

**Recommended path:** reuse the working venv for a small, capped candidate
only after its own small CPU/GPU parity and memory checks; budget **0–30 minutes**
to recheck environment/parity, **~8–20 minutes** for a TabM-sized *single*
fit (442 seconds previously, with preprocessing/export extra), and
**~1–2 hours** for bounded tuning plus measurement, subject to dataset and
model size. If the kernel/wheel pairing fails for a candidate, fall back
to CPU for a tiny smoke test (**~15–45 minutes**) and seek explicit approval
before a clean, supported Linux environment/rebuild (**~2–6 hours setup
estimate**, followed by separate training time); do not install drivers,
compile ROCm packages or assume GPU training will work merely because the
card is detected.

## Approved selection checkpoint (task 1.5)

The user approved this shortlist, validation budget and fresh-holdout policy at
the research checkpoint. This approval authorizes experiments within these
limits, **not** a deployment, new live API study, or a claim of verified
prospective five-hour accuracy.

**Shortlist for equal-input comparison:**

1. Existing TabM arrival output as the frozen reference; arrival-only, smaller TabM with
   train/station embeddings as a candidate (one bounded hyperparameter search).
2. A compact residual/RealMLP-style arrival network, with explicit unknown category codes,
   trained-only scaling, and a matching ONNX/WASM export (one bounded search).
3. A small training-only conditional arrival-delay histogram/backoff formula using the
   station, route/train, schedule time and service-day type, with a specified global fallback.
   This is a *candidate*, not a required baseline or a disguised actual-outcome average.

Defer FT-Transformer, propagation conditioned on observed upstream trains, and tree ensembles:
the first has unproven incremental benefit and web latency, the second needs unavailable
five-hour state, and the previous unconstrained LightGBM is oversized. Reconsider only
with a separate feasibility/approval decision. Do not infer a method's accuracy from papers.

**Frozen input/target draft:** one `RECORDED`, uncancelled, plausible observed arrival at a
unique `(service_date, trip/run, stop_sequence, station, scheduled_arrival_utc)`; no target-run
observations or departure labels. Use existing training-available train/station/line/direction,
category/operator, station location, train route position and stop count, service-date
weekday/holiday, per-run planned origin time and **planned arrival** local clock instead of
the departure-preferred shared clock. Preserve unknowns and missingness. Destination/variant
and extra live/network state are **excluded initially**: stable route linkage and as-of
timetable availability cannot yet be proven for these features. Audit parity of timetable
versus historical rows; disclose when a present-day pattern is used in a replay.

**Output/objective draft:** predict a finite arrival delay in minutes or an explicit missing
reason. TabM and MLP try median-oriented training and, only if a valid distribution is
learned, validation-selected highest-six-minute-mass points; histogram compares its
empirical-window point with its median. Preserve genuine ordered p25/p50/p75 only for
distributional models, alongside separately named point estimates; never label a
window-optimal point `p50`. Select architecture, backoff depth, smoothing, training loss
and point rule on **validation within-3 hit rate** (inclusive), use scored-instance MAE
then asset size as tie-breakers, and report 1/3/5-minute rates, misses and coverage on
identical eligible arrivals. A formula with an unseen key uses training-only broad fallback,
not a silent no-prediction or a test-day aggregate.

**Data policy:** use training dates 2026-08-24 through 2026-09-12 and validation dates
2026-09-13 through 2026-09-16 for initial search; September 17–22 is a *previously
inspected diagnostic slice only*. Before fitting, select and seal at least three *newly
ingested complete, uninspected* service dates after 2026-09-22 for final testing; the
September 23 stub is not eligible. If these dates are unavailable, stop after validation
and wait; do not rename a reused date "fresh." Ensure the fit's latest possible label
and any feature statistic predate each scored call's arrival-minus-five-hours cutoff;
for adjacent/cross-midnight dates either apply a conservative embargo or exclude the
affected evaluation rows. This is a retrospective **five-hour-cutoff replay** where
historical timetable snapshots cannot be reconstructed, **not** evidence of a logged
prospective forecast. To make that claim later, begin logging pre-cutoff predictions,
assets and schedule snapshots before arrivals occur.

**Runtime and compute draft:** run small CPU/GPU parity and a 1,000-row ONNX/WASM browser
prototype before full fitting. Reuse the installed ROCm venv for neural jobs, CPU for
statistics; export both networks to versioned ONNX, implement the histogram as versioned
browser lookup/JS with matching backend reference. Budget at most **12 total validation
trials** across the three candidates, **2 hours GPU + 1 hour CPU computation** across
those trials, **20 GiB host RAM**, **14 GiB GPU VRAM**, and **35,000,000 bytes** of
prediction-specific browser assets per candidate. Use a bounded, date-stratified training
subset for initial probes; confirm full-fit feasibility from measured throughput and
request a revised budget if needed. The earlier TabM fit took ~7.4 minutes; the
1–3-hour aggregate compute cap is a ceiling, not a forecast. Stop an oversized,
incompatible or parity-failing candidate; do not move `CURRENT`. No new live Timetables
API study is part of this approval. A later approved replacement must update CLI,
browser/API and the existing two-sided `live_accuracy.py` consumer together or gate
the study explicitly; deployment requires its **own** later approval.

## Decisions during implementation (2026-09-26)

- **Validation search approved (4/4/4, 12 trials):** histogram with minimum support 5 or 20,
  each with the window and median rules; TabM default quantile, default binned+window, smaller
  (width 128, 4 members, train embedding 8) and larger (width 512, 16 members, train embedding
  32) binned+window; residual MLP default quantile, default binned+window, default
  binned+median, and width 512 / 3 blocks binned+window. Provisional defaults: width 256,
  2 blocks, 8 members, train embedding 16, 2% unknown-category dropout on train/station/line
  (approved). Histogram day type stays holiday/normal; weekday is not added.
- **Fresh final-test days come from the public IRIS archive** (`dbrel archive-fetch` +
  `dbrel backfill`, raw responses replayed against their own recording time), because local
  collection stopped after the 2026-09-23 stub. The training/validation days were backfilled
  the same way, so the label source is unchanged. The days are sealed: no outcome is
  inspected before the locked evaluation.
- **Incumbent on identical rows:** the arrival dataset also stores v2's departure-preferred
  clock, so the deployed model is scored with exactly its original inputs.
- **Measured (task 3.2):** the finalization-time cutoff removes all of 2026-09-12 and about a
  third of 2026-09-11 from training history (490,468 of 7,188,388 rows); CPU/GPU parity and
  browser parity passed for every candidate; every prototype asset is under 35,000,000 bytes.
- **Fresh-holdout result and decision (tasks 4.2–4.3):** the frozen rank-1 candidate (residual
  MLP, width 512, 3 blocks, binned distribution, densest-6-minute-window point; ONNX 11.4 MB)
  scored once on 1,321,999 eligible arrivals of 2026-09-23..25: within 3 min 83.8% vs the
  incumbent's 75.1%, within 5 min 88.9% vs 85.0%, but within 1 min 24.4% vs 47.0% and MAE
  3.56 vs 2.97 min (the window rule's deliberate trade-off). Consistent on every date and
  segment. Anas reviewed this and requested **deployment** (2026-09-26); no further
  experimentation requested. Results: `data/ml/arrival_runs/validation-search-20260926/final.json`.
- **Deployed (task 5.2, 2026-09-26, approved by Anas):** artifact `data/models/v2/20260926T113337Z`
  (`arrival-net`, 11,453,745 bytes; reproduces the fresh-test predictions exactly on 20,000
  sampled rows). `CURRENT` moved atomically from `20260924T183944Z`; rollback verified with
  `dbrel.ml.arrival.promote(models_dir, "20260924T183944Z")` (both CLI paths checked), then
  re-promoted. Estimates are whole minutes (bin-aligned windows) and +3.0 is the most common
  value for near-punctual calls; departures are no longer predicted by the deployed model.
