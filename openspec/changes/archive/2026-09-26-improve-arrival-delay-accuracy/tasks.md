# Tasks

This is a research-first plan. Complete section 1 and obtain its explicit approval before
candidate implementation or expensive training. Later tasks are staged integration work; expand
their candidate-specific details at that checkpoint rather than choosing a model silently.
Research tasks 1.1–1.4 are documented in the design; implementation and training have not begun.

Before **every task**, tell the user the current duration estimate and its basis. Distinguish
hands-on work from unattended computation; revise estimates when measurements change them.
The ranges below are provisional planning estimates, not measured runtimes or promises.
The old 159-minute report covered a particular LightGBM run, not all candidate libraries; it
cannot justify halving the runtime of a new multi-candidate experiment.

## 1. Research and selection checkpoint — no implementation

- [x] 1.1 Audit the specific data questions in design D1/D2: arrival identity, observed-vs-estimated labels, arrival-specific scheduled time, route/destination linkage, training-history coverage and availability at a five-hour cutoff. Inspect `dataset.py`, `features.py`, `timetable.py`, `split.py` and relevant fixtures read-only. Verify: an evidence table names usable inputs, unavailable inputs, target filters and historical-replay limitations. Est.: 30–60 minutes.
- [x] 1.2 Research arrival-only TabM tuning, compact residual/RealMLP-style networks, a small tabular transformer and size-constrained tree/distillation alternatives. Check primary sources and actual ONNX Runtime Web constraints rather than claiming universal compatibility. Verify: a comparison records source URLs, fitting objective, data needs, expected size/runtime and supported versus unverified claims for each family. Est.: 45–90 minutes.
- [x] 1.3 Research conditional delay distributions, additive/interaction formulas and timetable-based stochastic propagation; assess hierarchical smoothing only as an optional candidate. Investigate median versus maximum-three-minute-probability point selection. Verify: the comparison explains how each method produces one call's forecast, its assumptions, missing-data behavior and dependence on unavailable live/network inputs. Est.: 45–90 minutes.
- [x] 1.4 Inspect the existing RX 9070 XT environment and check the exact OS/driver/Python/ROCm/torch combination against current AMD documentation, without installing or rebuilding anything. Verify: a sourced compatibility note records installed versions and a recommended reuse/fallback path with separate setup and training estimates. Est.: 20–40 minutes.
- [x] 1.5 Present the shortlist and bounded experiment plan for approval; make candidate-specific tasks concrete before coding. Resolve features, point/interval outputs, loss/selection rules, fresh holdout dates, runtime/export path, resource caps and any additional live-study consumer scope. Verify: explicit user approval is recorded and proposal/design/spec/tasks agree. Stop here if approval is absent. Est.: 20–40 minutes preparation, plus user review time.

## 2. Individual-arrival evaluation — after checkpoint approval

- [x] 2.1 Add an arrival-only experiment dataset view without destroying v2 caches or departure observations. Retain unique instance identity, cutoff and each recorded arrival delay; never substitute grouped means. Verify: tests preserve distinct 1/4/12-minute targets for repeated services and exclude invalid/cancelled arrivals independently of departure quality. Est.: 45–90 minutes.
- [x] 2.2 Enforce whole-date splits and forecast-cutoff provenance for parameters, vocabulary, preprocessing and historical statistics, including cross-midnight cutoffs. Verify: tests prevent target/later observations from entering inputs and correctly label unavailable historical timetable snapshots; previously inspected test dates are not labeled fresh. Est.: 45–90 minutes.
- [x] 2.3 Extend `metrics.py` with per-instance absolute errors and inclusive within-1/3/5-minute scores, coverage and eligible/scored/missing counts. Score before display rounding and count missing predictions as unsuccessful in the headline denominator. Verify: tests cover the 4-predicted/6-actual example, exactly-three-minute errors, all-missing and empty evaluations, and no dependence on stored-average availability. Est.: 45–75 minutes.
- [x] 2.4 Update experiment reports to retain individual forecast/outcome/error rows and show date, segment, day-type and hard-case breakdowns. Remove mandatory average-comparison tables from the new experiment path; keep the incumbent arrival reference on identical rows. Verify: a synthetic report's counts and percentages reproduce its saved per-instance rows and valid large delays remain included. Est.: 45–90 minutes.

## 3. Approved candidate adapters and resource probes

- [x] 3.1 Implement the approved arrival-only smaller TabM (estimated 1–3 hours), compact residual/RealMLP-style arrival network (estimated 1–3 hours), and training-only conditional arrival histogram with explicit backoff (estimated 1–3 hours), leaving the v2 predictor usable. Each adapter must return a finite arrival point or explicit missing reason and only genuine ordered percentiles where supported; test unknown categories, output shapes and the selected decision rule.
- [x] 3.2 For selected neural candidates, run the approved capped CPU/GPU forward/backward parity and memory probe in the verified environment before full training. For non-neural candidates, run their approved correctness/resource probe. Verify: saved results include numerical tolerance, device/software versions, host/VRAM peaks and measured throughput; failures stop the full run. Est.: 20–40 minutes hands-on plus 5–20 minutes computation per candidate.
- [x] 3.3 Implement resumable experiment checkpoints that include data, split, cutoff, feature, architecture and objective identities. Verify: interruption resumes completed trials without mixing settings and never changes `CURRENT`. Est.: 45–90 minutes.
- [x] 3.4 Prototype the approved browser export/runtime with small fitted assets before full training. Count all prediction-specific assets toward 35,000,000 bytes and report shared runtime assets separately. Verify: actual browser inference matches the reference within a recorded tolerance; oversized or unsupported artifacts remain deployment-ineligible. Est.: 45–120 minutes per runtime path.

## 4. Bounded experiments and final evidence

- [x] 4.1 Run the approved validation search through `dbrel ml launch`, ranking candidates by individual within-three-minute accuracy with coverage and secondary error summaries visible. Re-estimate duration from the probes immediately before launch and respect the approved cap. Verify: saved trial results identify validation dates and never use final-test outcomes for tuning or decision-rule selection. Est.: 20–40 minutes hands-on; provisionally 1–8 hours computation for the approved search, to be replaced by the measured estimate.
- [x] 4.2 Freeze the chosen settings and score the exported predictor plus incumbent once on the reserved fresh holdout. Record all individual outcomes, exclusions, misses and subgroup results without an averages-based promotion rule. Verify: the report is reproducible from saved rows and `CURRENT` is unchanged. Est.: 30–60 minutes hands-on plus 10–60 minutes computation; waiting for fresh data is separate.
- [x] 4.3 Present the actual per-arrival results and remaining limitations for user review, including an inconclusive/no-improvement outcome when appropriate. Verify: the report makes no per-train accuracy guarantee and records whether further experimentation or deployment is requested. Do not launch a live API study without an explicit run request. Est.: 15–30 minutes, plus user review time.

## 5. Deployment readiness and optional approved replacement

- [x] 5.1 Complete the approved arrival-only CLI/browser contract and consumer compatibility work, including origin/terminus handling, point-versus-percentile labels, missing predictions and old-artifact support. Resolve any live-study incompatibility within the scope approved at checkpoint 1.5; do not knowingly defer a break. Verify: targeted backend/frontend tests and a real browser check pass; departure averages and old artifacts remain intact. Est.: 1–3 hours, refined once the output contract is selected.
- [x] 5.2 Record readiness and the user's deployment decision. If replacement is approved, switch the complete versioned artifact atomically and verify rollback to the previous compatible artifact. Otherwise record no deployment and preserve `CURRENT`. Verify: size/parity/compatibility results and pointer state agree with the decision. Est.: 20–40 minutes, plus user review time.
