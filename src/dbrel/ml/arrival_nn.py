"""Arrival-only networks: a smaller TabM and a compact RealMLP-style residual MLP.

Needs torch (and `tabm` for arch="tabm"), so it runs only in the ROCm venv:

    data/build/torch-rocm/venv/bin/python ...

Inputs come from `arrival.fit_input_prep` / `arrival_inputs`, fitted on
training rows only. Slot 0 of every categorical is UNKNOWN; during training a
share `unknown_rate` of train/station/line slots is replaced by 0, so the
UNKNOWN embedding learns "a call like this one, but unseen" rather than
staying at its random initial value.

Heads:
- `quantile`: median plus two softplus gaps -> ordered p25/p50/p75 by
  construction; pinball loss; point rule `median`.
- `binned`: softmax over 1-minute delay bins (`arrival.bin_edges`); cross
  entropy; a genuine distribution, so the point can be `median` or `window`.

"RealMLP-style" here means robust scaling with smooth clipping (the `robust`
input prep), a learned per-input scale in front of the network and a residual
MLP. It is not the full RealMLP-TD recipe.

Early stopping keeps the epoch with the best validation within-3-minute share
(ties: lower MAE), the selection metric -- not quantile loss. No GPU tensor is
allowed to reach 2**31 bytes (ROCm on gfx1201 corrupts the tail beyond that).
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass

import numpy as np
import torch
from torch import nn

from dbrel.ml.arrival import (
    PERCENTILES, ArrivalForecast, arrival_inputs, bin_edges, bin_index, distribution_point,
)
from dbrel.ml.features import CATEGORICAL
from dbrel.ml.metrics import score_arrivals

ARCHS = ("tabm", "mlp")
HEADS = ("quantile", "binned")
UNKNOWN_DROPOUT_COLUMNS = ("train", "station", "line")
MAX_ACTIVATION_BYTES = 2 ** 30
NON_FINITE = "non_finite_output"


@dataclass(frozen=True)
class NetConfig:
    arch: str = "tabm"
    head: str = "quantile"
    point_rule: str = "median"
    hidden: int = 256
    blocks: int = 2
    dropout: float = 0.15
    embedding_max_dim: int = 32
    train_emb_dim: int | None = 16
    tabm_k: int = 8
    unknown_rate: float = 0.02
    lr: float = 2e-3
    weight_decay: float = 1e-2
    batch_size: int = 8192
    epochs: int = 12
    patience: int = 3
    seed: int = 0

    def __post_init__(self):
        if self.arch not in ARCHS or self.head not in HEADS:
            raise ValueError(f"unknown arch/head {self.arch!r}/{self.head!r}")
        if self.point_rule == "window" and self.head != "binned":
            raise ValueError("the window rule needs a learned distribution (head='binned')")
        if self.point_rule not in ("median", "window"):
            raise ValueError(f"unknown point rule {self.point_rule!r}")
        if not 0 <= self.unknown_rate < 1:
            raise ValueError("unknown_rate must be in [0, 1)")

    @property
    def members(self) -> int:
        return self.tabm_k if self.arch == "tabm" else 1

    def as_dict(self) -> dict:
        return asdict(self)


def embedding_dims(cfg: NetConfig, cards: list[int]) -> list[int]:
    dims = [max(2, min(cfg.embedding_max_dim, round(1.6 * c ** 0.56))) for c in cards]
    if cfg.train_emb_dim is not None:
        dims[CATEGORICAL.index("train")] = cfg.train_emb_dim
    return dims


class _Inputs(nn.Module):
    def __init__(self, cards, dims):
        super().__init__()
        self.embs = nn.ModuleList(nn.Embedding(c, d) for c, d in zip(cards, dims))

    def forward(self, cat, num):
        return torch.cat([e(cat[:, i]) for i, e in enumerate(self.embs)] + [num], dim=1)


class _Block(nn.Module):
    def __init__(self, width, dropout):
        super().__init__()
        self.net = nn.Sequential(nn.LayerNorm(width), nn.Linear(width, width), nn.SiLU(),
                                 nn.Dropout(dropout), nn.Linear(width, width))

    def forward(self, x):
        return x + self.net(x)


class ArrivalMLP(nn.Module):
    """Residual MLP with a learned per-input scale; output (batch, n_out)."""

    def __init__(self, cfg: NetConfig, cards, n_num, n_out):
        super().__init__()
        dims = embedding_dims(cfg, cards)
        width_in = sum(dims) + n_num
        self.inputs = _Inputs(cards, dims)
        self.scale = nn.Parameter(torch.ones(width_in))
        self.inp = nn.Sequential(nn.Linear(width_in, cfg.hidden), nn.SiLU())
        self.blocks = nn.Sequential(*[_Block(cfg.hidden, cfg.dropout) for _ in range(cfg.blocks)])
        self.head = nn.Sequential(nn.LayerNorm(cfg.hidden), nn.Linear(cfg.hidden, n_out))

    def forward(self, cat, num):
        return self.head(self.blocks(self.inp(self.inputs(cat, num) * self.scale)))


class ArrivalTabM(nn.Module):
    """TabM backbone over the same embeddings; output (batch, k, n_out)."""

    def __init__(self, cfg: NetConfig, cards, n_num, n_out):
        super().__init__()
        import tabm

        dims = embedding_dims(cfg, cards)
        self.k = cfg.tabm_k
        self.inputs = _Inputs(cards, dims)
        self.view = tabm.EnsembleView(k=cfg.tabm_k)
        self.backbone = tabm.make_tabm_backbone(
            d_in=sum(dims) + n_num, n_blocks=cfg.blocks, d_block=cfg.hidden, dropout=cfg.dropout,
            activation="SiLU", k=cfg.tabm_k, arch_type="tabm",
            start_scaling_init="random-signs", start_scaling_init_chunks=dims + [1] * n_num,
        )
        self.head = tabm.LinearEnsemble(cfg.hidden, n_out, k=cfg.tabm_k)

    def forward(self, cat, num):
        return self.head(self.backbone(self.view(self.inputs(cat, num))))


def build_net(cfg: NetConfig, cards: list[int], n_num: int, n_bins: int) -> nn.Module:
    n_out = 3 if cfg.head == "quantile" else n_bins
    return (ArrivalTabM if cfg.arch == "tabm" else ArrivalMLP)(cfg, cards, n_num, n_out)


def ordered_quantiles(raw):
    p50 = raw[..., 0]
    lo = p50 - nn.functional.softplus(raw[..., 1])
    hi = p50 + nn.functional.softplus(raw[..., 2])
    return torch.stack([lo, p50, hi], dim=-1)


def arrival_loss(cfg: NetConfig, raw, y, y_bin):
    """Every TabM member is scored on its own; y (B,), y_bin (B,) int64."""
    if raw.dim() == 3:
        k = raw.shape[1]
        raw = raw.reshape(-1, raw.shape[-1])
        y, y_bin = y.repeat_interleave(k), y_bin.repeat_interleave(k)
    if cfg.head == "binned":
        return nn.functional.cross_entropy(raw, y_bin)
    q = torch.tensor(PERCENTILES, device=raw.device, dtype=raw.dtype)
    diff = y.unsqueeze(-1) - ordered_quantiles(raw)
    return torch.maximum(q * diff, (q - 1) * diff).mean()


def safe_batch(cfg: NetConfig, n_bins: int, requested: int) -> int:
    per_row = max(cfg.hidden, n_bins) * 4 * cfg.members
    return max(1, min(requested, MAX_ACTIVATION_BYTES // per_row))


class ArrivalNet:
    """One network candidate: fit on training rows, early-stop on validation within-3."""

    def __init__(self, cfg: NetConfig, prep: dict, edges: np.ndarray | None = None):
        self.cfg = cfg
        self.prep = prep
        self.edges = bin_edges() if edges is None else np.asarray(edges, dtype=np.float64)
        self.n_bins = len(self.edges) - 1
        torch.manual_seed(cfg.seed)
        self.model = build_net(cfg, prep["cards"], len(prep["numeric"]), self.n_bins)
        self.history: list[dict] = []

    def n_parameters(self) -> int:
        return sum(p.numel() for p in self.model.parameters())

    def _raw(self, cat, num):
        raw = self.model(cat, num)
        if self.cfg.head == "binned":
            probs = torch.softmax(raw.float(), dim=-1)
            return probs.mean(dim=1) if probs.dim() == 3 else probs
        q = ordered_quantiles(raw.float())
        return q.mean(dim=1) if q.dim() == 3 else q  # a mean of ordered triples stays ordered

    def predict_arrays(self, cats: np.ndarray, nums: np.ndarray, device="cpu",
                       batch: int = 65536) -> ArrivalForecast:
        # A distribution is reduced to its point and percentiles batch by batch:
        # (rows x bins) float64 for a whole validation set is gigabytes.
        batch = safe_batch(self.cfg, self.n_bins, batch)
        self.model.to(device).eval()
        point = np.full(len(cats), np.nan)
        pct = np.full((len(cats), 3), np.nan)
        finite = np.zeros(len(cats), dtype=bool)
        with torch.no_grad():
            for s in range(0, len(cats), batch):
                c = torch.as_tensor(cats[s:s + batch], device=device)
                n = torch.as_tensor(nums[s:s + batch], device=device)
                raw = self._raw(c, n).cpu().numpy().astype(np.float64)
                ok = np.isfinite(raw).all(axis=1)
                idx = np.arange(s, s + len(raw))[ok]
                finite[idx] = True
                if not ok.any():
                    continue
                if self.cfg.head == "binned":
                    point[idx], pct[idx] = distribution_point(raw[ok], self.edges, self.cfg.point_rule)
                else:
                    pct[idx], point[idx] = raw[ok], raw[ok, 1]
        return ArrivalForecast.build(point, np.where(finite, "", NON_FINITE),
                                     self.cfg.point_rule, pct)

    def predict(self, encoded: dict[str, np.ndarray], device="cpu") -> ArrivalForecast:
        return self.predict_arrays(*arrival_inputs(encoded, self.prep), device=device)

    def fit(self, cats: np.ndarray, nums: np.ndarray, y: np.ndarray, train_rows: np.ndarray,
            val_rows: np.ndarray, device="cpu", *, log=None) -> list[dict]:
        cfg = self.cfg
        if safe_batch(cfg, self.n_bins, cfg.batch_size) != cfg.batch_size:
            raise ValueError("training batch would reach the 2 GiB ROCm activation fault")
        if not (np.isfinite(y[train_rows]).all() and np.isfinite(y[val_rows]).all()):
            raise ValueError("every training and validation row needs a recorded arrival")
        torch.manual_seed(cfg.seed)
        model = self.model.to(device)
        cats_t = torch.as_tensor(cats[train_rows], device=device)
        nums_t = torch.as_tensor(nums[train_rows], device=device)
        y_t = torch.as_tensor(y[train_rows].astype(np.float32), device=device)
        yb_t = torch.as_tensor(bin_index(y[train_rows], self.edges), device=device)
        drop_cols = torch.tensor([CATEGORICAL.index(c) for c in UNKNOWN_DROPOUT_COLUMNS],
                                 device=device)
        steps = math.ceil(len(train_rows) / cfg.batch_size)
        decay = [p for n, p in model.named_parameters() if p.ndim == 2 and not n.startswith("inputs.")]
        other = [p for n, p in model.named_parameters() if not (p.ndim == 2 and not n.startswith("inputs."))]
        opt = torch.optim.AdamW([{"params": decay, "weight_decay": cfg.weight_decay},
                                 {"params": other, "weight_decay": 0.0}], lr=cfg.lr)
        sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=cfg.lr, total_steps=cfg.epochs * steps)
        autocast = device != "cpu"
        best, best_state, bad = (-1.0, -math.inf), None, 0
        self.history = []
        for epoch in range(cfg.epochs):
            model.train()
            started = time.monotonic()
            perm = torch.randperm(len(train_rows), device=device)
            total = torch.zeros((), device=device)
            for s in range(steps):
                b = perm[s * cfg.batch_size:(s + 1) * cfg.batch_size]
                cat = cats_t[b]
                if cfg.unknown_rate:
                    hit = torch.rand(len(b), len(drop_cols), device=device) < cfg.unknown_rate
                    cat = cat.clone()
                    cat[:, drop_cols] = torch.where(hit, 0, cat[:, drop_cols])
                with torch.autocast("cuda", dtype=torch.bfloat16, enabled=autocast):
                    raw = model(cat, nums_t[b])
                loss = arrival_loss(cfg, raw.float(), y_t[b], yb_t[b])
                opt.zero_grad(set_to_none=True)
                loss.backward()
                opt.step()
                sched.step()
                total += loss.detach()
            val = score_arrivals(y[val_rows], self.predict_arrays(cats[val_rows], nums[val_rows], device).point)
            entry = {"epoch": epoch + 1, "train_loss": float(total) / steps,
                     "val_within_3_min": val["within_3_min"], "val_mae_scored": val["mae_scored"],
                     "val_coverage": val["coverage"], "seconds": round(time.monotonic() - started, 1)}
            self.history.append(entry)
            if log:
                log(entry)
            rank = (val["within_3_min"], -(val["mae_scored"] if val["mae_scored"] is not None else math.inf))
            if rank > best:
                best, bad = rank, 0
                best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            else:
                bad += 1
                if bad >= cfg.patience:
                    break
        model.load_state_dict(best_state)
        return self.history
