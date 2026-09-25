"""Experiment: a neural network (MLP with entity embeddings) on the AMD GPU.

Run with the SEPARATE venv that holds PyTorch-ROCm (data/build/torch-rocm/venv):

    data/build/torch-rocm/venv/bin/python scripts/experiment_nn.py [--epochs 12] [--check-only]

Why a network at all: tree boosting is many small, sequential steps and ran
slower on this GPU than on two CPU threads (design.md D10). A network's
training is large matrix multiplications -- what a GPU is built for -- and
high-cardinality categoricals (66k trains, 5k stations) become learned
embeddings rather than split searches.

Same data as the bake-off: the rows, features (design.md D1), date split,
training-day vocabulary, quantiles and metrics all come from dbrel.ml, so the
test scores line up with the bake-off report. Nothing from the stored averages.

Model: every categorical gets an embedding (index 0 = UNKNOWN); numerics are
standardised with a missing-value indicator, and times of day also enter as
sin/cos so 23:59 sits next to 00:00. A residual MLP feeds one output block per
side (departure, arrival), each producing the median plus two softplus gaps,
so p25 <= p50 <= p75 by construction -- no crossing to sort out afterwards.
Loss: mean pinball over the three levels, masked per side where a row has no
target for that side. AdamW, one-cycle learning rate, early stopping on
validation mean pinball (the bake-off's winner criterion).

`--check-only` just verifies the GPU computes what the CPU computes: the same
initialised network, the same batch, forward and backward on both devices.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbrel.ml.dataset import load_dataset  # noqa: E402
from dbrel.ml.features import CATEGORICAL, FEATURES  # noqa: E402
from dbrel.ml.metrics import QUANTILES, score_with_breakdowns  # noqa: E402
from dbrel.ml.split import masks_for, restrict_to_training_vocab, split_dates  # noqa: E402

NUMERIC = [f for f in FEATURES if f not in CATEGORICAL]
CYCLIC = ("sched_min", "trip_start_min")

# Approved by Anas 2026-09-24, with his "stronger regularisation" choice: weight
# decay 1e-2 on the dense weights only (not embeddings, biases or LayerNorm) and
# dropout 0.15.
HP = {
    "embedding_max_dim": 64,     # dim = min(max, round(1.6 * n ** 0.56)), the fastai rule
    "hidden": 512,
    "blocks": 3,                 # residual blocks
    "dropout": 0.15,
    "batch_size": 8192,
    "lr": 2e-3,                  # one-cycle peak
    "weight_decay": 1e-2,        # dense weights only
    "epochs": 12,
    "patience": 3,               # epochs without validation improvement
    "arch": "mlp",               # or "tabm"
    "tabm_k": 16,                # TabM ensemble members
    "train_emb_dim": None,       # own size for the 61k-value train number (None: the rule)
}


def emb_dim(n: int) -> int:
    return max(2, min(HP["embedding_max_dim"], round(1.6 * n ** 0.56)))


def column_dims(cards) -> list[int]:
    """Embedding size per categorical column (CATEGORICAL order)."""
    dims = [emb_dim(c) for c in cards]
    if HP["train_emb_dim"] is not None:
        dims[CATEGORICAL.index("train")] = HP["train_emb_dim"]
    return dims


def build_arrays(ds, masks, features):
    """Categorical indices (shifted so 0 = UNKNOWN), standardised numerics, targets."""
    cats = np.column_stack([features[c].astype(np.int64) + 1 for c in CATEGORICAL])
    cards = [int(cats[:, i].max()) + 1 for i in range(len(CATEGORICAL))]
    num_cols, names = [], []
    train = masks["train"]
    for name in NUMERIC:
        v = features[name].astype(np.float64)
        miss = np.isnan(v)
        mu, sd = np.nanmean(v[train]), np.nanstd(v[train]) or 1.0
        num_cols.append(np.where(miss, 0.0, (v - mu) / sd))
        names.append(name)
        if miss.any():
            num_cols.append(miss.astype(np.float64))
            names.append(f"{name}_missing")
        if name in CYCLIC:
            angle = 2 * math.pi * np.nan_to_num(v) / 1440.0
            num_cols += [np.sin(angle), np.cos(angle)]
            names += [f"{name}_sin", f"{name}_cos"]
    nums = np.column_stack(num_cols).astype(np.float32)
    y = np.column_stack([ds.dep_min, ds.arr_min]).astype(np.float32)  # NaN = no target
    return cats, cards, nums, names, y


def make_model(torch, cards, n_num):
    nn = torch.nn

    class Block(nn.Module):
        def __init__(self, width):
            super().__init__()
            self.net = nn.Sequential(nn.LayerNorm(width), nn.Linear(width, width), nn.SiLU(),
                                     nn.Dropout(HP["dropout"]), nn.Linear(width, width))

        def forward(self, x):
            return x + self.net(x)

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            dims = column_dims(cards)
            self.embs = nn.ModuleList(nn.Embedding(c, d) for c, d in zip(cards, dims))
            width_in = sum(dims) + n_num
            self.inp = nn.Sequential(nn.Linear(width_in, HP["hidden"]), nn.SiLU())
            self.blocks = nn.Sequential(*[Block(HP["hidden"]) for _ in range(HP["blocks"])])
            self.head = nn.Sequential(nn.LayerNorm(HP["hidden"]), nn.Linear(HP["hidden"], 6))

        def forward(self, cat, num):
            x = torch.cat([e(cat[:, i]) for i, e in enumerate(self.embs)] + [num], dim=1)
            out = self.head(self.blocks(self.inp(x))).view(-1, 2, 3)  # side x (median, gap-, gap+)
            p50 = out[..., 0]
            lo = p50 - torch.nn.functional.softplus(out[..., 1])
            hi = p50 + torch.nn.functional.softplus(out[..., 2])
            return torch.stack([lo, p50, hi], dim=-1)  # (batch, side, quantile)

    return Net()


def make_tabm(torch, cards, n_num):
    """TabM (Gorishniy et al., ICLR 2025): k MLPs sharing weights via BatchEnsemble.

    The `tabm` package one-hot encodes categoricals, which for 61k train numbers
    and 5k stations would mean ~67k input columns; here each categorical instead
    goes through the same learned embedding as the MLP, and the embeddings plus
    numerics feed TabM's ensemble backbone. Output: (batch, k, side, quantile);
    each member is trained on its own loss, predictions average the members.
    """
    import tabm

    nn = torch.nn
    dims = column_dims(cards)

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.embs = nn.ModuleList(nn.Embedding(c, d) for c, d in zip(cards, dims))
            self.view = tabm.EnsembleView(k=HP["tabm_k"])
            self.backbone = tabm.make_tabm_backbone(
                d_in=sum(dims) + n_num, n_blocks=HP["blocks"], d_block=HP["hidden"],
                dropout=HP["dropout"], activation="SiLU", k=HP["tabm_k"], arch_type="tabm",
                start_scaling_init="random-signs", start_scaling_init_chunks=dims + [1] * n_num,
            )
            self.head = tabm.LinearEnsemble(HP["hidden"], 6, k=HP["tabm_k"])

        def forward(self, cat, num):
            x = torch.cat([e(cat[:, i]) for i, e in enumerate(self.embs)] + [num], dim=1)
            out = self.head(self.backbone(self.view(x))).view(x.shape[0], HP["tabm_k"], 2, 3)
            p50 = out[..., 0]
            lo = p50 - torch.nn.functional.softplus(out[..., 1])
            hi = p50 + torch.nn.functional.softplus(out[..., 2])
            return torch.stack([lo, p50, hi], dim=-1)

    return Net()


def build_model(torch, cards, n_num):
    return make_tabm(torch, cards, n_num) if HP["arch"] == "tabm" else make_model(torch, cards, n_num)


def members_mean(pred):
    """TabM predicts per ensemble member; the prediction is their mean (still ordered)."""
    return pred.mean(dim=1) if pred.dim() == 4 else pred


def pinball(torch, pred, y):
    """Mean pinball over quantiles, masked where y is NaN. pred (B,2,3), y (B,2)."""
    q = torch.tensor(QUANTILES, device=pred.device).view(1, 1, 3)
    if pred.dim() == 4:  # TabM: (batch, k, side, q) -- every member is scored
        k = pred.shape[1]
        pred = pred.reshape(-1, 2, 3)
        y = y.repeat_interleave(k, dim=0)
    mask = ~torch.isnan(y)
    target = torch.nan_to_num(y).unsqueeze(-1)
    diff = target - pred
    loss = torch.maximum(q * diff, (q - 1) * diff).mean(-1)
    return (loss * mask).sum() / mask.sum().clamp(min=1)


def device_check(torch, cats, cards, nums, y) -> dict:
    """Same weights and batch on CPU and GPU: forward output and gradients must match."""
    torch.manual_seed(0)
    model = build_model(torch, cards, nums.shape[1])
    idx = np.random.default_rng(0).choice(len(y), 8192, replace=False)
    out = {}
    for device in ("cpu", "cuda"):
        m = build_model(torch, cards, nums.shape[1])
        m.load_state_dict(model.state_dict())
        m = m.to(device).eval()
        c = torch.as_tensor(cats[idx], device=device)
        n = torch.as_tensor(nums[idx], device=device)
        t = torch.as_tensor(y[idx], device=device)
        loss = pinball(torch, m(c, n), t)
        loss.backward()
        grad = torch.cat([p.grad.flatten() for p in m.parameters() if p.grad is not None])
        out[device] = (float(loss.detach()), grad.cpu().double())
    loss_rel = abs(out["cuda"][0] - out["cpu"][0]) / abs(out["cpu"][0])
    grad_rel = float((out["cuda"][1] - out["cpu"][1]).norm() / out["cpu"][1].norm())
    return {"loss_cpu": out["cpu"][0], "loss_gpu": out["cuda"][0], "loss_relative_difference": loss_rel,
            "grad_relative_difference": grad_rel, "passed": loss_rel < 1e-4 and grad_rel < 1e-3}


# ROCm on this card (torch 2.10+rocm7.0, gfx1201) computes the far end of a
# tensor wrongly once it reaches 2**31 bytes -- measured 2026-09-24: TabM's
# first layer at 65,536 rows x 16 members x 512 wide (exactly 2 GiB) returned
# garbage for the last rows and correct values for the first. Every forward
# pass here keeps each activation well under that.
MAX_ACTIVATION_BYTES = 2 ** 30


def safe_batch(requested: int) -> int:
    per_row = HP["hidden"] * 4 * (HP["tabm_k"] if HP["arch"] == "tabm" else 1)
    return max(1, min(requested, MAX_ACTIVATION_BYTES // per_row))


def predict(torch, model, cats, nums, rows, device, batch=65536):
    batch = safe_batch(batch)
    model.eval()
    outs = []
    with torch.no_grad():
        for s in range(0, len(rows), batch):
            r = rows[s:s + batch]
            outs.append(members_mean(model(torch.as_tensor(cats[r], device=device),
                                           torch.as_tensor(nums[r], device=device))).float().cpu().numpy())
    return np.concatenate(outs)  # (n, side, quantile)


def train_loop(torch, cards, cats, nums, y, train_rows, val_rows, cats_t, nums_t, y_t, device,
               *, on_epoch=None, log=None):
    """Train per HP with early stopping on validation mean pinball.

    Returns (best validation mean pinball, best state_dict, per-epoch history).
    `on_epoch(epoch, val) -> bool` may stop training early (Optuna pruning).
    """
    torch.manual_seed(0)
    model = build_model(torch, cards, nums.shape[1]).to(device)
    assert safe_batch(HP["batch_size"]) == HP["batch_size"], "training batch would reach the 2 GiB fault"
    steps_per_epoch = math.ceil(len(train_rows) / HP["batch_size"])
    decay = [p for n, p in model.named_parameters() if p.ndim == 2 and not n.startswith("embs.")]
    no_decay = [p for n, p in model.named_parameters() if not (p.ndim == 2 and not n.startswith("embs."))]
    opt = torch.optim.AdamW([{"params": decay, "weight_decay": HP["weight_decay"]},
                             {"params": no_decay, "weight_decay": 0.0}], lr=HP["lr"])
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=HP["lr"], total_steps=HP["epochs"] * steps_per_epoch)

    best, best_state, bad, history = math.inf, None, 0, []
    for epoch in range(HP["epochs"]):
        model.train()
        t = time.monotonic()
        perm = torch.randperm(len(train_rows), device=device)
        total = torch.zeros((), device=device)
        for s in range(steps_per_epoch):
            b = perm[s * HP["batch_size"]:(s + 1) * HP["batch_size"]]
            with torch.autocast("cuda", dtype=torch.bfloat16):
                pred = model(cats_t[b], nums_t[b])
            loss = pinball(torch, pred.float(), y_t[b])
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sched.step()
            total += loss.detach()
        total = float(total)
        pv = predict(torch, model, cats, nums, val_rows, device)
        val = float(np.nanmean([_mean_pinball(y[val_rows, s], pv[:, s]) for s in (0, 1)]))
        history.append({"epoch": epoch + 1, "train_loss": total / steps_per_epoch, "val_mean_pinball": val,
                        "seconds": round(time.monotonic() - t, 1)})
        if log:
            log(f"epoch {epoch + 1}: train {total / steps_per_epoch:.4f}, val mean pinball {val:.4f}, "
                f"{time.monotonic() - t:.0f}s", flush=True)
        if val < best - 1e-4:
            best, bad = val, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= HP["patience"]:
                break
        if on_epoch is not None and on_epoch(epoch + 1, val):
            break
    return best, best_state, history


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=HP["epochs"])
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--arch", choices=("mlp", "tabm"), default="mlp")
    parser.add_argument("--tabm-k", type=int, default=HP["tabm_k"])
    parser.add_argument("--batch-size", type=int, default=HP["batch_size"])
    args = parser.parse_args()
    HP["epochs"] = args.epochs
    HP["arch"] = args.arch
    HP["tabm_k"] = args.tabm_k
    HP["batch_size"] = args.batch_size

    import torch

    torch.set_num_threads(args.threads)
    started = time.monotonic()
    ds = load_dataset(ROOT / "data" / "ml")
    split = split_dates(ds.meta["rows_per_date"])
    masks = masks_for(ds.day, ds.meta["dates"], split)
    features, _ = restrict_to_training_vocab(ds.features, ds.meta["vocab"], masks["train"])
    cats, cards, nums, num_names, y = build_arrays(ds, masks, features)
    print(f"device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'}; "
          f"torch {torch.__version__}; data {cats.shape[0]:,} rows, cards {cards}, "
          f"{nums.shape[1]} numeric columns ({time.monotonic() - started:.0f}s)", flush=True)

    check = device_check(torch, cats, cards, nums, y)
    print(f"device check: {check}", flush=True)
    result = {"hp": HP, "split": split.as_dict(), "check": check, "cards": cards, "numeric": num_names}
    out_dir = ROOT / "data" / "ml" / "experiments"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"nn_{HP['arch']}.json"
    if args.check_only or not check["passed"]:
        path.write_text(json.dumps(result, indent=1))
        return 0 if check["passed"] else 1

    device = "cuda"
    train_rows = np.flatnonzero(masks["train"])
    val_rows = np.flatnonzero(masks["validation"])
    test_rows = np.flatnonzero(masks["test"])
    cats_t = torch.as_tensor(cats[train_rows], device=device)
    nums_t = torch.as_tensor(nums[train_rows], device=device)
    y_t = torch.as_tensor(y[train_rows], device=device)

    best, best_state, history = train_loop(
        torch, cards, cats, nums, y, train_rows, val_rows, cats_t, nums_t, y_t, device, log=print)
    model = build_model(torch, cards, nums.shape[1]).to(device)
    model.load_state_dict(best_state)

    pt = predict(torch, model, cats, nums, test_rows, device)
    pv = predict(torch, model, cats, nums, val_rows, device)
    result["history"] = history
    result["sides"] = {}
    for s, side in enumerate(("dep", "arr")):
        has = ~np.isnan(y[test_rows, s])
        rows = test_rows[has]
        groups = {"segment": ds.segment[rows], "holiday": features["is_holiday"][rows].astype(int),
                  "date": np.array(ds.meta["dates"], dtype=object)[ds.day[rows]]}
        vhas = ~np.isnan(y[val_rows, s])
        result["sides"][side] = {
            "validation_mean_pinball": _mean_pinball(y[val_rows[vhas], s], pv[vhas, s]),
            "test": score_with_breakdowns(y[rows, s], pt[has, s], groups),
        }
        o = result["sides"][side]["test"]["overall"]
        print(f"{side}: test MAE p50 {o['mae_p50']:.3f}, band coverage {o['band_coverage']:.1%}, "
              f"val mean pinball {result['sides'][side]['validation_mean_pinball']:.4f}", flush=True)
    result["seconds"] = round(time.monotonic() - started, 1)
    torch.save(best_state, out_dir / f"nn_{HP['arch']}_model.pt")
    path.write_text(json.dumps(result, indent=1, default=float))
    print(f"done in {result['seconds'] / 60:.0f} min -> {path}", flush=True)
    return 0


def _mean_pinball(y, pred) -> float:
    has = ~np.isnan(y)
    y, pred = y[has], pred[has]
    return float(np.mean([np.mean(np.maximum(a * (y - pred[:, i]), (a - 1) * (y - pred[:, i])))
                          for i, a in enumerate(QUANTILES)]))


if __name__ == "__main__":
    raise SystemExit(main())
