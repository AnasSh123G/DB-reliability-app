## Decisions (as carried out)

| Decision | Choice | Why |
| --- | --- | --- |
| What to deploy | A retrain of the recipe up to 2026-09-19, not the experiment's model trained up to 2026-08-22 | Deploying the experiment model would discard the newest month, the data the 3× weighting is meant to emphasize |
| Training rows | The live model's exact row population, with corrected labels joined on service date and stop ID | Same dates, inputs and notice rates as the live model. The two models differ only in labels, weighting and model type |
| Missing corrected labels | 385,255 rows from 2026-08-21/22 (cut differently by the two sets' finalization cutoffs) were corrected with the unchanged version-2 policy | Every training label follows one definition. The overlap with the earlier correction agreed exactly |
| Epochs | 12, no selection | The strong run's discrete view had selected epoch 12, the last |
| Evidence | Offline head-to-head on the locked week 2026-09-20..26 | Neither model trained on it, and both predict the same rows. The live model reproduced its recorded score first. Replaces the planned live-accuracy run: faster, larger and fully paired |
| Labels for the verdict | Corrected | They measure trains against the right plan. Original-label results are reported alongside |
| Archive folder window for the test-week and 08-21/22 corrections | Read folders d-2..d+2 for the same calendar window | From mid-September the mirror stores late and early hours in neighbouring folders. The correction rules are otherwise unchanged |
| Deploy source | A clean worktree of `e8fc543` | The main checkout had unrelated uncommitted `src/dbrel` edits that `fly deploy` would have shipped |

## Open question (not resolved here)

- The TabM reference `20260924T183944Z` named in the delay-model spec is missing from disk and
  from git history. That requirement can't be met until the user decides to amend it or recover
  the artifact.
