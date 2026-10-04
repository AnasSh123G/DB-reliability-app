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

Early stopping keeps the epoch with the lowest validation MAE of the point
estimate (ties: higher within-3 share), the selection metric -- not the
training loss. No GPU tensor is allowed to reach 2**31 bytes (ROCm on gfx1201
corrupts the tail beyond that).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import torch
from torch import nn

from dbrel.ml.arrival import (
    PERCENTILES, ArrivalForecast, arrival_inputs, bin_edges, distribution_point,
)
from dbrel.ml.features import CATEGORICAL
from dbrel.ml.metrics import score_arrivals

ARCHS = ("tabm", "mlp")
HEADS = ("quantile", "binned")
UNKNOWN_DROPOUT_COLUMNS = ("train", "station", "line")
MAX_ACTIVATION_BYTES = 2 ** 30
ROCM_TENSOR_LIMIT = 2 ** 31
BLOCK_ROWS = 1 << 20
PREDICT_BATCH = 65536
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
    patience: int = 6  # 12 epochs on a OneCycle schedule: 3 stopped the dry run (best epoch 4) before the low-LR phase
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


def estimate_model_bytes(cfg: NetConfig, prep: dict, n_bins: int | None = None) -> int:
    """float32 weights of the exported network, counted on the meta device (nothing allocated)."""
    n_bins = len(bin_edges()) - 1 if n_bins is None else n_bins
    with torch.device("meta"):
        model = build_net(cfg, prep["cards"], len(prep["numeric"]), n_bins)
    return 4 * (sum(p.numel() for p in model.parameters()) + sum(b.numel() for b in model.buffers()))


def estimate_asset_bytes(cfg: NetConfig, prep: dict, vocab: dict, n_bins: int | None = None) -> int:
    """What the browser downloads: the weights plus the manifest's prep and vocabulary."""
    return (estimate_model_bytes(cfg, prep, n_bins) + len(json.dumps(prep, indent=1))
            + len(json.dumps(vocab, indent=1)))


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


def bin_index_torch(y, edges):
    """`arrival.bin_index` on the device (edges are whole minutes, exact in float32)."""
    return (torch.searchsorted(edges, y, right=True) - 1).clamp_(0, len(edges) - 2)


def epoch_rank(val: dict) -> tuple[float, float]:
    """Higher is better: lowest validation MAE, then the within-3 share."""
    mae = val["mae_scored"] if val["mae_scored"] is not None else math.inf
    return (-mae, val["within_3_min"] or 0.0)


def safe_batch(cfg: NetConfig, n_bins: int, requested: int) -> int:
    per_row = max(cfg.hidden, n_bins) * 4 * cfg.members
    return max(1, min(requested, MAX_ACTIVATION_BYTES // per_row))


class BatchSource:
    """Rows of (cats, nums, y) -- in memory or memory-mapped -- read in fixed blocks.

    `rows` picks (sorted) rows of the arrays; None means all of them in order,
    which on a memmap reads each block as one contiguous range. An epoch
    visits the blocks in a random order and every row exactly once.
    """

    def __init__(self, cats: np.ndarray, nums: np.ndarray, y: np.ndarray,
                 rows: np.ndarray | None = None, block_rows: int = BLOCK_ROWS):
        if not (len(cats) == len(nums) == len(y)):
            raise ValueError("cats, nums and y must have one row each per arrival")
        self.cats, self.nums, self.y = cats, nums, y
        self.rows = None if rows is None else np.asarray(rows, dtype=np.int64)
        self.block_rows = int(block_rows)
        self.n = len(y) if self.rows is None else len(self.rows)
        self.n_num = nums.shape[1]

    def __len__(self) -> int:
        return self.n

    def spans(self) -> list[tuple[int, int]]:
        return [(s, min(s + self.block_rows, self.n)) for s in range(0, self.n, self.block_rows)]

    def read(self, start: int, stop: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        idx = slice(start, stop) if self.rows is None else self.rows[start:stop]
        return (np.ascontiguousarray(self.cats[idx], dtype=np.int32),
                np.ascontiguousarray(self.nums[idx], dtype=np.float32),
                np.asarray(self.y[idx], dtype=np.float32))

    def blocks(self, rng: np.random.Generator | None = None):
        spans = self.spans()
        order = rng.permutation(len(spans)) if rng is not None else range(len(spans))
        for i in order:
            yield self.read(*spans[i])

    def targets(self) -> np.ndarray:
        return np.concatenate([b[2] for b in self.blocks()]) if self.n else np.zeros(0, np.float32)


def tensor_bytes(cfg: NetConfig, n_bins: int, n_num: int, d_in: int, block_rows: int) -> dict[str, int]:
    """Bytes of the largest tensors one training or validation step creates."""
    batch = cfg.batch_size
    return {
        "block cats (int64 on device)": block_rows * len(CATEGORICAL) * 8,
        "block nums": block_rows * n_num * 4,
        "block targets (y, bin)": block_rows * 8,
        "batch input view": batch * cfg.members * d_in * 4,
        "batch activations": batch * cfg.members * max(cfg.hidden, n_bins) * 4,
        "prediction activations": safe_batch(cfg, n_bins, PREDICT_BATCH) * cfg.members * max(cfg.hidden, n_bins) * 4,
    }


def check_tensor_sizes(cfg: NetConfig, n_bins: int, n_num: int, d_in: int, block_rows: int) -> None:
    """Refuse, before the first step, any setup with a tensor at or above 2**31 bytes."""
    sizes = tensor_bytes(cfg, n_bins, n_num, d_in, block_rows)
    too_big = {k: v for k, v in sizes.items() if v >= ROCM_TENSOR_LIMIT}
    if too_big:
        raise ValueError("a tensor would reach the 2 GiB ROCm fault: "
                         + ", ".join(f"{k} {v / 2**30:.2f} GiB" for k, v in too_big.items()))
    if safe_batch(cfg, n_bins, cfg.batch_size) != cfg.batch_size:
        raise ValueError("training batch would reach the 2 GiB ROCm activation fault")


class ArrivalNet:
    """One network candidate: fit on training rows, early-stop on validation MAE."""

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

    def d_in(self) -> int:
        return sum(embedding_dims(self.cfg, self.prep["cards"])) + len(self.prep["numeric"])

    def predict_arrays(self, cats: np.ndarray, nums: np.ndarray, device="cpu",
                       batch: int = PREDICT_BATCH) -> ArrivalForecast:
        # A distribution is reduced to its point and percentiles batch by batch:
        # (rows x bins) float64 for a whole validation set is gigabytes.
        batch = safe_batch(self.cfg, self.n_bins, batch)
        self.model.to(device).eval()
        point = np.full(len(cats), np.nan)
        pct = np.full((len(cats), 3), np.nan)
        finite = np.zeros(len(cats), dtype=bool)
        with torch.no_grad():
            for s in range(0, len(cats), batch):
                c = torch.as_tensor(np.asarray(cats[s:s + batch]), device=device).long()
                n = torch.as_tensor(np.asarray(nums[s:s + batch], dtype=np.float32), device=device)
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

    def predict_source(self, source: BatchSource, device="cpu") -> ArrivalForecast:
        """`predict_arrays` over a batch source, block by block in row order."""
        parts = [self.predict_arrays(c, n, device) for c, n, _ in source.blocks()]
        if not parts:
            return ArrivalForecast.build(np.zeros(0), np.zeros(0, dtype=str), self.cfg.point_rule,
                                         np.zeros((0, 3)))
        return ArrivalForecast.build(
            np.concatenate([p.point for p in parts]), np.concatenate([p.missing_reason for p in parts]),
            self.cfg.point_rule, np.concatenate([p.percentiles for p in parts]))

    def predict(self, encoded: dict[str, np.ndarray], device="cpu") -> ArrivalForecast:
        return self.predict_arrays(*arrival_inputs(encoded, self.prep), device=device)

    def fit(self, cats: np.ndarray, nums: np.ndarray, y: np.ndarray, train_rows: np.ndarray,
            val_rows: np.ndarray, device="cpu", *, log=None) -> list[dict]:
        """`fit_source` on chosen rows of in-memory (or memory-mapped) arrays."""
        return self.fit_source(BatchSource(cats, nums, y, np.sort(train_rows)),
                               BatchSource(cats, nums, y, np.sort(val_rows)), device, log=log)

    def fit_source(self, train: BatchSource, val: BatchSource | None, device="cpu", *,
                   log=None, epochs: int | None = None) -> list[dict]:
        """Fit block by block; early-stop on validation MAE, restoring the best epoch.

        Without `val` (the final refit on training + validation), it runs
        exactly `epochs` epochs and keeps the last.
        """
        cfg = self.cfg
        if val is None and epochs is None:
            raise ValueError("a refit without validation needs a fixed epoch count")
        epochs = cfg.epochs if epochs is None else epochs
        check_tensor_sizes(cfg, self.n_bins, train.n_num, self.d_in(), train.block_rows)
        y_val = val.targets() if val is not None else None
        for block in train.blocks():
            if not np.isfinite(block[2]).all():
                raise ValueError("every training and validation row needs a recorded arrival")
        if y_val is not None and not np.isfinite(y_val).all():
            raise ValueError("every training and validation row needs a recorded arrival")
        torch.manual_seed(cfg.seed)
        rng = np.random.default_rng(cfg.seed)
        model = self.model.to(device)
        edges_t = torch.as_tensor(self.edges, dtype=torch.float32, device=device)
        drop_cols = torch.tensor([CATEGORICAL.index(c) for c in UNKNOWN_DROPOUT_COLUMNS],
                                 device=device)
        steps = sum(math.ceil((b - a) / cfg.batch_size) for a, b in train.spans())
        decay = [p for n, p in model.named_parameters() if p.ndim == 2 and not n.startswith("inputs.")]
        other = [p for n, p in model.named_parameters() if not (p.ndim == 2 and not n.startswith("inputs."))]
        opt = torch.optim.AdamW([{"params": decay, "weight_decay": cfg.weight_decay},
                                 {"params": other, "weight_decay": 0.0}], lr=cfg.lr)
        sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=cfg.lr, total_steps=max(1, epochs * steps))
        on_gpu = device != "cpu"
        best, best_state, bad = (-math.inf, -math.inf), None, 0
        self.history = []
        for epoch in range(epochs):
            model.train()
            started = time.monotonic()
            total = torch.zeros((), device=device)
            for cats_b, nums_b, y_b in train.blocks(rng):
                cats_t, nums_t, y_t = (torch.from_numpy(a) for a in (cats_b, nums_b, y_b))
                if on_gpu:
                    cats_t, nums_t, y_t = (t.pin_memory() for t in (cats_t, nums_t, y_t))
                cats_t = cats_t.to(device, non_blocking=True).long()
                nums_t = nums_t.to(device, non_blocking=True)
                y_t = y_t.to(device, non_blocking=True)
                yb_t = bin_index_torch(y_t, edges_t)
                perm = torch.randperm(len(y_t), device=device)
                for s in range(0, len(y_t), cfg.batch_size):
                    b = perm[s:s + cfg.batch_size]
                    cat = cats_t[b]
                    if cfg.unknown_rate:
                        hit = torch.rand(len(b), len(drop_cols), device=device) < cfg.unknown_rate
                        cat[:, drop_cols] = torch.where(hit, 0, cat[:, drop_cols])
                    with torch.autocast("cuda", dtype=torch.bfloat16, enabled=on_gpu):
                        raw = model(cat, nums_t[b])
                    loss = arrival_loss(cfg, raw.float(), y_t[b], yb_t[b])
                    opt.zero_grad(set_to_none=True)
                    loss.backward()
                    opt.step()
                    sched.step()
                    total += loss.detach()
                del cats_t, nums_t, y_t, yb_t
            entry = {"epoch": epoch + 1, "train_loss": float(total) / max(1, steps)}
            if val is not None:
                scored = score_arrivals(y_val, self.predict_source(val, device).point)
                entry.update(val_within_3_min=scored["within_3_min"], val_mae_scored=scored["mae_scored"],
                             val_coverage=scored["coverage"])
            entry["seconds"] = round(time.monotonic() - started, 1)
            self.history.append(entry)
            if log:
                log(entry)
            if val is None:
                continue
            rank = epoch_rank(scored)
            if rank > best:
                best, bad = rank, 0
                best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            else:
                bad += 1
                if bad >= cfg.patience:
                    break
        if best_state is not None:
            model.load_state_dict(best_state)
        return self.history

    def best_epoch(self) -> int:
        """The epoch `fit_source` kept (1-based), for refitting the same length without validation."""
        scored = [h for h in self.history if "val_mae_scored" in h]
        if not scored:
            return len(self.history)
        return max(scored, key=lambda h: epoch_rank({"mae_scored": h["val_mae_scored"],
                                                      "within_3_min": h["val_within_3_min"]}))["epoch"]


ONNX_TOLERANCE = {"quantiles": 1e-4, "probs": 1e-5}  # minutes / probability, ONNX vs PyTorch


def export_onnx(net: ArrivalNet, cats: np.ndarray, nums: np.ndarray, out: Path) -> dict:
    """Write `out/model.onnx` and `out/prep.json`; check ONNX Runtime against PyTorch on these rows.

    The output is `quantiles` (rows, 3) or `probs` (rows, bins), TabM members averaged.
    """
    import onnx
    import onnxruntime

    output = "probs" if net.cfg.head == "binned" else "quantiles"

    class Exported(nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, cat, num):
            return net._raw(cat, num)

    cats = np.ascontiguousarray(cats, dtype=np.int64)
    nums = np.ascontiguousarray(nums, dtype=np.float32)
    model = net.model.cpu().eval()
    c, n = torch.as_tensor(cats), torch.as_tensor(nums)
    with torch.no_grad():
        reference = net._raw(c, n).numpy()
    out.mkdir(parents=True)
    torch.onnx.export(Exported(model).eval(), (c[:64], n[:64]), out / "model.onnx",
                      input_names=["cat", "num"], output_names=[output],
                      dynamic_axes={"cat": {0: "rows"}, "num": {0: "rows"}, output: {0: "rows"}},
                      dynamo=False)
    onnx.checker.check_model(str(out / "model.onnx"))
    session = onnxruntime.InferenceSession(str(out / "model.onnx"), providers=["CPUExecutionProvider"])
    got = session.run([output], {"cat": cats, "num": nums})[0]
    diff = float(np.abs(got - reference).max())
    (out / "prep.json").write_text(json.dumps({
        "prep": net.prep, "config": net.cfg.as_dict(), "output": output,
        "edges": net.edges.tolist() if output == "probs" else None,
    }, indent=1))
    raw = got.astype(np.float64)
    if output == "probs":
        point, pct = distribution_point(raw, net.edges, net.cfg.point_rule)
    else:
        pct, point = raw, raw[:, 1]
    torch_forecast = net.predict_arrays(cats, nums)
    return {
        "kind": "onnx", "files": ["model.onnx", "prep.json"], "output": output,
        "point_rule": net.cfg.point_rule, "scaling": net.prep["scaling"], "n_parameters": net.n_parameters(),
        "onnx_vs_torch_max_abs": diff, "onnx_tolerance": ONNX_TOLERANCE[output],
        "onnx_passed": diff <= ONNX_TOLERANCE[output],
        "points_differing_torch_vs_onnx": int(np.count_nonzero(
            np.abs(torch_forecast.point - point.astype(np.float32)) > 1e-4)),
        "_expected": {"raw": got.astype(np.float32), "point": point.astype(np.float32),
                      "pct": pct.astype(np.float32)},
    }
