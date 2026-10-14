"""Calendar-month trip-date weights and unreduced existing arrival losses."""
from __future__ import annotations
from calendar import monthrange
from datetime import date
import numpy as np


def policy(last_date: str, amplitude: float = .5):
    end=date.fromisoformat(last_date)
    year=end.year-(end.month==1);month=12 if end.month==1 else end.month-1
    start=date(year,month,min(end.day,monthrange(year,month)[1]))
    if not np.isfinite(amplitude) or amplitude < 0:
        raise ValueError('amplitude must be finite and nonnegative')
    return {'kind':'last-calendar-month-linear','start':start.isoformat(),'end':end.isoformat(),
            'amplitude':float(amplitude),'date_unit':'training trip service date',
            'normalization':'weighted batch mean / fixed full-training mean weight'}


def weights(days, record):
    days=np.asarray(days)
    start=int(np.datetime64(record['start'],'D').astype(int));end=int(np.datetime64(record['end'],'D').astype(int))
    if end<=start or np.any(days>end):raise ValueError('weights require training dates no later than policy end')
    if days.dtype.kind not in 'iu':raise ValueError('service dates must be integer epoch days')
    return (1 + record['amplitude']*np.clip((days.astype(np.float64)-start)/(end-start),0,1)).astype(np.float32)


def per_arrival_loss(cfg, raw, y, y_bin):
    import torch
    from torch.nn import functional as F
    from dbrel.ml.arrival_nn import ordered_quantiles, PERCENTILES
    if raw.ndim==3:
        members=raw.shape[1]
        flat=raw.reshape(-1,raw.shape[-1]); yy=y.repeat_interleave(members);yb=y_bin.repeat_interleave(members)
    elif raw.ndim==2:
        members=1;flat=raw;yy=y;yb=y_bin
    else:raise ValueError('raw output must have batch and class axes')
    if cfg.head=='binned':
        loss=F.cross_entropy(flat,yb,reduction='none')
    else:
        q=torch.tensor(PERCENTILES,device=flat.device,dtype=flat.dtype)
        diff=yy.unsqueeze(-1)-ordered_quantiles(flat)
        loss=torch.maximum(q*diff,(q-1)*diff).mean(dim=-1)
    return loss.reshape(len(y),members).mean(dim=1)


def weighted_loss(cfg, raw, y, y_bin, weight):
    import torch
    if weight.shape!=y.shape or not torch.isfinite(weight).all() or (weight<=0).any():
        raise ValueError('positive finite row-aligned weights required')
    per=per_arrival_loss(cfg,raw,y,y_bin)
    return (per*weight).sum()/weight.sum()


def normalized_weighted_loss(cfg, raw, y, y_bin, weight, dataset_mean_weight):
    """Keep temporal multipliers across batches, including constant-weight ones."""
    import torch
    if not np.isfinite(dataset_mean_weight) or dataset_mean_weight <= 0:
        raise ValueError('dataset mean weight must be positive and finite')
    if len(y) == 0 or weight.shape != y.shape or not torch.isfinite(weight).all() or (weight <= 0).any():
        raise ValueError('positive finite row-aligned weights required')
    per = per_arrival_loss(cfg, raw, y, y_bin)
    return (per * weight).mean() / dataset_mean_weight
