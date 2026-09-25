"""Time-boxed Optuna search for TabM on the AMD GPU.

Run with the PyTorch-ROCm venv (data/build/torch-rocm/venv):

    data/build/torch-rocm/venv/bin/python scripts/tune_tabm.py [--budget-min 120]

Search ranges approved by Anas on 2026-09-24, including his addition of a
separate embedding size for the 61k-value train number, the likeliest source of
TabM's overfitting. Each trial trains up to 12 epochs, with early stopping after
2 epochs without improvement, and reports its validation score after every
epoch so Optuna's median pruner can stop clearly worse trials. Trials are
chosen on validation days only.

Model size matters because the model is meant to load in a browser (Anas,
2026-09-24): every trial records its float16 file size, trials over
`MAX_SIZE_MB` are rejected, and the result is an accuracy-vs-size frontier --
the best trial (by validation) under each of `FRONTIER_MB`. Each distinct
frontier model is trained once more and scored once on the test days, exactly
as experiment_nn.py scores a single run. Results go to
data/ml/experiments/nn_tabm_tuned.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import experiment_nn as E  # noqa: E402
from dbrel.ml.dataset import load_dataset  # noqa: E402
from dbrel.ml.memory import format_memory  # noqa: E402
from dbrel.ml.metrics import score_with_breakdowns  # noqa: E402
from dbrel.ml.split import masks_for, restrict_to_training_vocab, split_dates  # noqa: E402


MAX_SIZE_MB = 35.0
FRONTIER_MB = (2, 5, 10, 20, 35)


def model_size_mb(torch, cards, n_num) -> float:
    """float16 size of the model E.HP describes: 2 bytes per parameter."""
    model = E.build_model(torch, cards, n_num)
    return 2 * sum(p.numel() for p in model.parameters()) / 1e6


def space(trial) -> dict:
    return {
        "lr": trial.suggest_float("lr", 3e-4, 3e-3, log=True),
        "dropout": trial.suggest_float("dropout", 0.0, 0.3),
        "weight_decay": trial.suggest_float("weight_decay", 1e-4, 1e-1, log=True),
        "hidden": trial.suggest_categorical("hidden", [256, 512, 768, 1024]),
        "blocks": trial.suggest_int("blocks", 2, 4),
        "embedding_max_dim": trial.suggest_categorical("embedding_max_dim", [16, 32, 64]),
        "train_emb_dim": trial.suggest_int("train_emb_dim", 8, 32),
        "tabm_k": trial.suggest_categorical("tabm_k", [8, 16, 32]),
        "batch_size": trial.suggest_categorical("batch_size", [4096, 8192, 16384]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-min", type=float, default=120)
    parser.add_argument("--max-trials", type=int, default=60)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()

    import optuna
    import torch

    torch.set_num_threads(args.threads)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    started = time.monotonic()
    ds = load_dataset(ROOT / "data" / "ml")
    split = split_dates(ds.meta["rows_per_date"])
    masks = masks_for(ds.day, ds.meta["dates"], split)
    features, _ = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    E.HP.update(arch="tabm", epochs=12, patience=2)
    cats, cards, nums, _, y = E.build_arrays(ds, masks, features)
    device = "cuda"
    train_rows = np.flatnonzero(masks["train"])
    val_rows = np.flatnonzero(masks["validation"])
    test_rows = np.flatnonzero(masks["test"])
    cats_t = torch.as_tensor(cats[train_rows], device=device)
    nums_t = torch.as_tensor(nums[train_rows], device=device)
    y_t = torch.as_tensor(y[train_rows], device=device)
    print(f"data on {device}: {len(train_rows):,} training rows, memory {format_memory()}", flush=True)
    base = dict(E.HP)
    deadline = started + args.budget_min * 60
    history = []

    def objective(trial):
        E.HP.clear()
        E.HP.update(base, **space(trial))
        if E.safe_batch(E.HP["batch_size"]) < E.HP["batch_size"]:
            raise optuna.TrialPruned()  # would reach the 2 GiB fault
        size = model_size_mb(torch, cards, nums.shape[1])
        trial.set_user_attr("size_mb", size)
        if size > MAX_SIZE_MB:
            print(f"trial {trial.number}: rejected, {size:.1f} MB > {MAX_SIZE_MB} MB", flush=True)
            raise optuna.TrialPruned()
        t = time.monotonic()

        def on_epoch(epoch, val):
            trial.report(val, epoch)
            return trial.should_prune() or time.monotonic() >= deadline

        best, _, hist = E.train_loop(torch, cards, cats, nums, y, train_rows, val_rows,
                                     cats_t, nums_t, y_t, device, on_epoch=on_epoch)
        pruned = trial.should_prune()
        history.append({"number": trial.number, "value": best, "pruned": pruned, "size_mb": round(size, 2),
                        "epochs": len(hist), "minutes": round((time.monotonic() - t) / 60, 1),
                        "params": trial.params})
        print(f"trial {trial.number}: {best:.4f}{' (pruned)' if pruned else ''}, {size:.1f} MB, {len(hist)} epochs, "
              f"{(time.monotonic() - t) / 60:.1f} min, memory {format_memory()}, {trial.params}", flush=True)
        torch.cuda.empty_cache()
        if pruned:
            raise optuna.TrialPruned()
        return best

    study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(seed=0),
                                pruner=optuna.pruners.MedianPruner(n_startup_trials=3, n_warmup_steps=2))
    # The untuned run's settings go first, so the search starts from a known point.
    study.enqueue_trial({"lr": 2e-3, "dropout": 0.15, "weight_decay": 1e-2, "hidden": 512, "blocks": 3,
                         "embedding_max_dim": 64, "train_emb_dim": 32, "tabm_k": 16, "batch_size": 8192})
    study.optimize(objective, n_trials=args.max_trials,
                   timeout=max(1.0, deadline - time.monotonic()))

    complete = [t for t in study.trials if t.state.name == "COMPLETE"]
    frontier = {}
    for limit in FRONTIER_MB:
        fits = [t for t in complete if t.user_attrs["size_mb"] <= limit]
        if fits:
            frontier[limit] = min(fits, key=lambda t: t.value)
    result = {"search": history, "n_trials": len(complete),
              "n_pruned": len([t for t in study.trials if t.state.name == "PRUNED"]),
              "max_size_mb": MAX_SIZE_MB, "frontier": {}, "models": {}}
    for limit, trial in frontier.items():
        result["frontier"][str(limit)] = trial.number
    out = ROOT / "data" / "ml" / "experiments"
    for trial in {t.number: t for t in frontier.values()}.values():
        print(f"final fit of trial {trial.number} ({trial.user_attrs['size_mb']:.1f} MB, "
              f"val {trial.value:.4f}), memory {format_memory()} ...", flush=True)
        E.HP.clear()
        E.HP.update(base, **trial.params)
        best, state, hist = E.train_loop(torch, cards, cats, nums, y, train_rows, val_rows,
                                         cats_t, nums_t, y_t, device, log=print)
        model = E.build_model(torch, cards, nums.shape[1]).to(device)
        model.load_state_dict(state)
        pt = E.predict(torch, model, cats, nums, test_rows, device)
        pv = E.predict(torch, model, cats, nums, val_rows, device)
        entry = {"params": trial.params, "size_mb": trial.user_attrs["size_mb"],
                 "final_history": hist, "sides": {}}
        for s, side in enumerate(("dep", "arr")):
            has = ~np.isnan(y[test_rows, s])
            rows = test_rows[has]
            groups = {"segment": ds.segment[rows], "holiday": features["is_holiday"][rows].astype(int),
                      "date": np.array(ds.meta["dates"], dtype=object)[ds.day[rows]]}
            vhas = ~np.isnan(y[val_rows, s])
            entry["sides"][side] = {
                "validation_mean_pinball": E._mean_pinball(y[val_rows[vhas], s], pv[vhas, s]),
                "test": score_with_breakdowns(y[rows, s], pt[has, s], groups),
            }
            o = entry["sides"][side]["test"]["overall"]
            print(f"  trial {trial.number} {side}: test MAE p50 {o['mae_p50']:.3f}, "
                  f"band coverage {o['band_coverage']:.1%}", flush=True)
        result["models"][str(trial.number)] = entry
        torch.save(state, out / f"nn_tabm_tuned_trial{trial.number}.pt")
        del model, state
        torch.cuda.empty_cache()
    result["minutes"] = round((time.monotonic() - started) / 60, 1)
    (out / "nn_tabm_tuned.json").write_text(json.dumps(result, indent=1, default=float))
    print(f"done in {result['minutes']:.0f} min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
