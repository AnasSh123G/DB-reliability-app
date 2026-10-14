---
title: "Compact browser delay models: investigation and experiments"
kind: spec
comments: none
---

Investigation completed 2026-10-13 against DB-reliability-app commit `2d6b28f`. Experiments are offline and uncommitted. The served model pointer stays at `20261004T213106Z`. Results below describe this snapshot, not future code or data.

## Constraints and intent

| Item | Decision |
| --- | --- |
| Desired metric | Percentage of arrival predictions whose absolute error is at most five minutes |
| Size | Around 35 MB or less; user explicitly allows modest flexibility |
| Compatibility | Keep the existing API paths, browser input names, and forecast object shape |
| Resource control | One experiment at a time; private systemd cgroup with `MemoryMax=8G`, `MemorySwapMax=0`; four CPU threads |
| Existing data | Read-only reuse of full-year shards, prepared inputs, and prior trial checkpoints; do not copy or load the entire dataset |
| Deployment | No promotion, deployment, commits, or database writes during this investigation |

## How the current model is made

```mermaid
flowchart LR
  A[IRIS recorded arrival outcomes] --> B[Monthly full-history shards]
  B --> C[Chronological training split]
  C --> D[Training vocabulary, scaling and historical notice rates]
  D --> E[Residual MLP with categorical embeddings]
  E --> F[Ordered quantile head and pinball loss]
  F --> G[Select by validation MAE]
  G --> H[Refit on training plus validation]
  H --> I[Export model.onnx plus manifest]
  I --> J[Server encodes cat and num inputs]
  J --> K[Browser ONNX Runtime Web WASM]
  K --> L[Arrival forecast: point, p25, p75, rule]
```

| Property | Deployed artifact |
| --- | --- |
| Architecture | Residual MLP, width 512, three blocks, learned per-input scale, SiLU, LayerNorm, dropout 0.15 |
| Parameters | 3,795,190 |
| Size | ONNX weights 15,191,410 bytes; model plus manifest 15,904,942 bytes. WASM runtime is separate. |
| Inputs | `cat`: int64 `[N,8]`; `num`: float32 `[N,26]` |
| Categories | Station, train, line, category, operator, state, weekday, direction; embedding slot zero means unknown |
| Numerics | Calendar/holiday, schedule and trip start, route position, coordinates, six historical construction/disruption rates. Cyclic time-of-year inputs were removed from the winner. |
| Output | `quantiles`: float32 `[N,3]`, ordered p25/p50/p75; current point is p50; arrivals only |
| Training | Pinball loss; AdamW and OneCycle learning rate; UNKNOWN dropout for train/station/line; 12 epochs |
| Recorded validation | 7,967,853 arrivals; 87.8093% within five minutes; MAE 2.5902 minutes |
| Recorded final test | 2,210,206 arrivals on 2026-09-20..26; 87.6204% within five minutes; MAE 2.5810 minutes |

The full-history dataset contains 99,566,282 eligible arrivals from 2025-11 through September 2026. Search training covers 2025-11-03..2026-08-22 (88,948,878 finalized rows for the winning input configuration), followed by four validation weeks and the test week. Splits keep whole service dates together. Training runs must be finalized before the evaluated forecast cutoff. Notice rates exclude a training row's own calendar month; validation/test only use training-period counts. A fingerprint prevents serving with changed historical notice totals.

Only recorded, eligible, non-cancelled arrivals are scored. Accuracy is conditional on that population: it does not include cancellation prediction or unrecorded events. The final score is dominated by regional and S-Bahn services. Long-distance test accuracy is only 47.43%, versus 84.11% regional and 91.98% S-Bahn.

The source identifies the score as historical replay at a five-hour forecast cutoff. It does not verify that the present timetable matches the timetable available at each historical cutoff, and it is not a logged prospective forecast.

Key implementation files: `src/dbrel/ml/full_history.py`, `full_year.py`, `full_year_search.py`, `arrival_nn.py`, `arrival.py`, `notice_rates.py`; `src/dbrel/web/queries.py`; `bahn-buddy-predict/src/lib/tabm.ts`. Despite its filename, `tabm.ts` supports the current MLP as well as TabM and distribution models.

## What prior searches already established

All rows below are the original 7,967,853-row validation period. Size estimates and measurements are distinguished in the original ranking.

| Candidate | Within five minutes | MAE, minutes | Approximate size |
| --- | --- | --- | --- |
| MLP 512, no time-of-year inputs, current winner | 87.81% | 2.590 | 15.77 MB estimated before final refit |
| MLP 512 with time-of-year inputs | 86.90% | 2.660 | 15.77 MB estimated |
| TabM 256, 8 members | 87.06% | 2.680 | 9.57 MB estimated |
| TabM 512, 16 members | 86.15% | 2.754 | 10.77 MB estimated |
| MLP with a 150-bin distribution, median decision | 87.45% | 2.871 | 16.07 MB estimated |
| Conditional histogram, median | 87.32% | 2.822 | 47.10 MB measured; exceeds budget |

A width-1024 MLP was skipped at an estimated 35,089,491 bytes: just 89,491 bytes above the strict 35,000,000-byte gate. The user's flexible size limit makes that candidate worth testing, but it has no measured improvement yet. The existing MLP is described as “RealMLP-style,” not the complete RealMLP-TD recipe.

## New diagnostic results

The existing validation period was split into calibration (2026-08-23..09-05) and a later probe (09-06..19, 4,241,218 arrivals). The original search already used the entire validation period. These results are therefore exploratory validation diagnostics, not new independent test results. No tuning is performed on the previously sealed test week.

| Prediction method, identical later probe rows | Within five minutes | Within three minutes | MAE, minutes |
| --- | --- | --- | --- |
| Current search MLP, median | 87.38% | 78.92% | 2.626 |
| Existing distribution MLP, median | 87.00% | 77.72% | 2.915 |
| Same distribution, most likely ±3-minute window | 89.90% | 85.14% | 3.523 |
| Same distribution, most likely ±5-minute window | 91.41% | 37.78% | 4.524 |
| Constant four-minute prediction, diagnostic baseline | 87.92% | 49.39% | 4.519 |
| LightGBM, three quantile models, one-million-row training sample | 85.81% | 77.51% | 3.015 |

The ±5-minute rule was best by within-five accuracy on the first 14 days and also beat the median on the later probe. Its forecasts are deliberately shifted toward the centre of a high-probability interval, so they can overstate small delays and lose precision. A window point is not the median. Constant baselines expose how a generous tolerance metric can reward inaccurate point forecasts; they are not deployment recommendations.

The distribution network's newly exported ONNX weighs 15,446,104 bytes. Python ONNX/PyTorch probability parity passed at maximum absolute difference `7.51e-6` (tolerance `1e-5`). The actual unchanged frontend predictor passed in headless Chromium with ONNX Runtime Web 1.30.0 for median, ±3-minute and ±5-minute metadata. Across 1,000 rows the maximum difference from Python for point/p25/p75 was `0.000976` minutes, below the `0.001`-minute tolerance; individual and batch predictions agreed exactly and output shapes/rules matched. The fixture's first 1,000 validation rows exercise runtime parity, not population accuracy. Single-call p95 was about 0.3 ms on this desktop with one WASM thread; this excludes real download time and is not a mobile benchmark.

LightGBM pilot details: 1,000,000 sampled training rows spread over the same 88,948,878-row history; 150,000 calibration rows from the first 14 validation days; native categorical features; 63 leaves; 400 boosting rounds for each of p25/p50/p75, four CPU threads. All models reached 400 rounds, so further fitting could change the result. The three text artifacts weigh 10,451,934, 35,073,015 and 41,457,574 bytes: 86,982,523 bytes total. This is text serialization, not a measured browser ONNX download; categorical split encoding can change export size substantially. Quantiles crossed on 8.18% of rows before sorting. The sorted median lost on both within-five accuracy and MAE. This sampled pilot is not an equal-training-volume comparison and does not establish that the tree family cannot win.

A minimal `ai.onnx.ml.TreeEnsembleRegressor` version-3 model also ran correctly in the installed browser WASM runtime. That verifies basic tree operator availability, not conversion of the full categorical LightGBM candidate. No full tree export was claimed or promoted.

Memory for this screen: peak RSS 1.531 GiB; private cgroup peak 1.809 GiB; job swap disabled; 45 seconds. Full probability matrices were never materialized: inference ran in 8,192-row batches.

LightGBM pilot: peak process RSS 1.010 GiB, private cgroup peak 6.308 GiB (the cgroup also counts charged filesystem cache), 404 seconds, job swap disabled. Browser check: cgroup peak about 993 MiB under a separate 2 GiB cap, job swap disabled. These are job figures, not a claim that other processes on the machine used no swap.

## Candidates worth pursuing

| Approach | Why it fits this case | Evidence / remaining work |
| --- | --- | --- |
| Distribution MLP with an explicitly chosen window rule | Existing model, compact ONNX, directly aligns with tolerance accuracy; same forecast schema already supports `rule: window` | Demonstrated validation gains above; quantify point-precision and interval-calibration tradeoffs before promotion |
| Better-tuned or wider MLP; full RealMLP recipe | Lowest deployment friction; existing training pipeline already streams the full history | Try width 1024 under the flexible size limit, longer training, tuned regularization and several seeds. [RealMLP research](https://arxiv.org/abs/2407.04491) motivates a stronger recipe, not a rail-specific performance guarantee. |
| TabM-mini with numeric feature embeddings | Parameter-efficient ensembling can remain compact | Existing plain TabM trials lost locally. A materially different configuration merits a bounded trial; [official TabM implementation](https://github.com/yandex-research/tabm) recommends considering numeric embeddings and describes mini variants and their compute tradeoffs. |
| LightGBM quantile models | Strong tabular baseline, categorical interactions and non-linear thresholds; bounded CPU sampling can train safely | This pilot lost and its text files exceed the budget. A next trial should limit high-cardinality split sets or use leakage-safe target encodings, tune leaves/rounds and recent-history weighting. [LightGBM parameters](https://lightgbm.readthedocs.io/en/latest/Parameters.html) support quantile loss. Exact export, size and latency must be measured if a model first wins offline. |
| CatBoost | Ordered categorical statistics are relevant to station/train/line identity | [CatBoost ONNX documentation](https://catboost.ai/docs/en/concepts/apply-onnx-ml) limits export to numerical features; requires a tested numerical encoding or custom export before it fits this deployment. |
| Distill a strong tree/ensemble teacher into the current MLP | Preserve small ONNX and browser interface even if a teacher is too large or slow | Only worth attempting once a teacher measurably wins. [DeepGBM research](https://www.microsoft.com/en-us/research/publication/deepgbm-a-deep-learning-framework-distilled-by-gbdt-for-online-prediction-tasks/) supports the general idea, not guaranteed preservation of this model's quantiles. |

The app uses WASM. Both the installed runtime's README and [ONNX Runtime Web's official source](https://github.com/microsoft/onnxruntime/blob/main/js/web/README.md) state support for `ai.onnx.ml`, so tree models should not be ruled out on operator-domain grounds. The exact converted categorical model must still pass the pinned runtime's browser check. WebGPU has a different, restricted operator set.

## Compatibility and acceptance before serving anything

Keep `/api/model/<stamp>/model.onnx`, `/api/model/<stamp>/meta.json`, board/route API paths, `model_stamp`, `model_input.cat`, and `model_input.num`. Forecast objects retain arrival `point_min`, `p25_min`, `p75_min`, and `rule`; absent departure forecasts remain absent. A quantile model returns ordered `[N,3]`; a distribution model returns `[N,150]` probabilities and existing metadata describes the decision rule.

The ±3-minute window already works through the existing Python and browser paths. The browser accepts `window_half_min` metadata, but Python currently uses a default of three and artifact saving overwrites this field with three. Serving a ±5-minute window would require a small consistency fix across artifact saving, Python inference and browser checks; API paths and output shapes can stay the same. Do not label a window point as p50.

Before promotion: choose the precision/tolerance tradeoff; select settings on validation only; evaluate once on newer untouched complete service dates; report within-one/three/five, MAE, coverage, segment results and quantile calibration; check Python/ONNX/browser parity, model size, and browser latency. Current dataset's final test dates were already inspected. Training and validation input fingerprints must match their checkpoints.

The read-only source database check found only 70,250 stop rows on 2026-09-27, of which 48,795 had recorded non-cancelled arrivals, and no later stop rows. That partial day cannot supply the next untouched full test week. Nothing was added to `inspected_dates.json`.

## Recommended next experiments

1. If a few-minute tolerance is the intended product behavior, prioritize a distribution MLP without time-of-year features, select checkpoints explicitly by within-five accuracy, and compare the existing ±3-minute window against ±5. Keep the precision and display-bias tradeoff visible. The existing ±3 rule requires the least serving work.
2. If point precision must stay at least as good, prioritize width 1024 and a stronger MLP training recipe, multiple seeds and a small ensemble only if it measurably improves the precision/tolerance frontier. Do not spend the full size budget on an ensemble merely because it fits: mixtures of the existing no-season and seasonal MLPs gave negligible gains or losses here.
3. Improve long-distance modeling separately and always report segments. Its low accuracy is masked by the much larger S-Bahn population. Conditional heads or balanced training are hypotheses, not demonstrated improvements.
4. Train tree teachers or distill them only after they win an offline chronological comparison. Quantization can make a winning network smaller or allow an ensemble; it does not by itself improve predictions and needs numerical/browser checks.

None of the new probes establishes an independently tested replacement for the deployed artifact. The strongest measured result is a tolerance/precision tradeoff within the existing compact neural family.

## Reproducing the bounded probes

Initial investigation scripts, now in this repository: `scripts/explore_compact_delay.py` and `scripts/explore_delay_browser.py`. The following arguments reproduce this frozen search snapshot; use a new private capped service per command and a fresh output directory for any repeat. The example's `EXPERIMENT` identifies the completed run and must be changed to repeat it. The scripts refuse an uncapped or swap-enabled cgroup. They never promote a model or write to the source dataset.

```bash
# From the primary DB reliability repository.
PROJECT=/home/anas/dev/DB-reliability-app
cd "$PROJECT"
EXPERIMENT=/home/anas/dev/DB-reliability-app/data/ml/compact_training_20261013/initial_probes
SCRIPT="$PWD/scripts/explore_compact_delay.py"
TRIALS="$PROJECT/data/ml/full_year/runs/compact-2026-09-26/trials"

systemd-run --user --wait --pipe --slice=background.slice \
  -p MemoryMax=8G -p MemorySwapMax=0 -p Nice=10 -p "WorkingDirectory=$PROJECT" \
  /usr/bin/env OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 \
  "$PROJECT/data/build/torch-rocm/venv/bin/python" "$SCRIPT" screen \
  --prepared /media/E/dbrel-ml/prepared --trials "$TRIALS" --out "$EXPERIMENT"

systemd-run --user --wait --pipe --slice=background.slice \
  -p MemoryMax=8G -p MemorySwapMax=0 -p Nice=10 -p "WorkingDirectory=$PROJECT" \
  /usr/bin/env OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 \
  "$PROJECT/.venv/bin/python" "$SCRIPT" trees \
  --prepared /media/E/dbrel-ml/prepared --trials "$TRIALS" --out "$EXPERIMENT"

systemd-run --user --wait --pipe --slice=background.slice \
  -p MemoryMax=2G -p MemorySwapMax=0 -p Nice=10 \
  "$PROJECT/.venv/bin/python" "$PWD/scripts/explore_delay_browser.py" \
  --runtime-root "$PROJECT" --experiment "$EXPERIMENT"
```

Check currently available memory before each launch; leave several GiB for other applications and reduce the cap/sample if headroom has fallen. The lightweight optional tree operator fixture was generated separately; the browser script checks it when `tree.onnx` exists in the experiment directory.

Initial binary outputs and JSON results now live under `data/ml/compact_training_20261013/initial_probes`; essential results are recorded above for host transfer. The initial investigation created two research scripts; the subsequent training work is described in [the training plan](compact-training-plan.md). Application and deployed-model files have no edits. Existing arrival deployment, feature and notice-rate tests passed: **50 tests**. Both scripts compile, all three capped jobs completed successfully, and no commits were created.
