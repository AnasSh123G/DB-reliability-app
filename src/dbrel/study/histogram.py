"""The live accuracy study's histogram: how many minutes each predictor was off, per range.

Built from a study folder's `comparisons.csv` (one row per stop), so it can be redrawn for any
past run without fetching anything again.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from dbrel.config import TZ

HISTOGRAM_NAME = "histogram.html"
TEMPLATE = Path(__file__).with_name("histogram_template.html")
BINS = ((0, 1), (1, 3), (3, 5), (5, 10), (10, 20), (20, None))  # minutes off; lower bound open


def _counts(errors: list[float]) -> list[int]:
    out = []
    for lo, hi in BINS:
        out.append(sum(1 for e in errors
                       if (e >= lo if lo == 0 else e > lo) and (hi is None or e <= hi)))
    return out


def histogram_data(study_dir: Path) -> dict:
    with (study_dir / "comparisons.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    # v2 studies store the median as tabm_p50; arrival-only ones a window point as model_point
    point = "tabm_p50" if rows and "tabm_p50" in rows[0] else "model_point"
    data = {}
    for side in ("both", "dep", "arr"):
        rs = [r for r in rows if (side == "both" or r["side"] == side)
              and r.get(point) and r["average_min"]]
        real = [float(r["real_delay_min"]) for r in rs]
        data[side] = {
            "n": len(rs),
            "tabm": _counts([abs(float(r[point]) - y) for r, y in zip(rs, real)]),
            "average": _counts([abs(float(r["average_min"]) - y) for r, y in zip(rs, real)]),
        }
    delays = [float(r["real_delay_min"]) for r in rows]
    data["real"] = {"n": len(delays), "max": max(delays, default=0),
                    **{f"over{m}": sum(d > m for d in delays) for m in (5, 15, 30, 60)}}
    result = json.loads((study_dir / "result.json").read_text())
    finished = datetime.strptime(study_dir.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    data["meta"] = {
        "stations": f"{result['stations_sampled']:,}",
        "when": finished.astimezone(TZ).strftime("finishing %d %b %Y at %H:%M"),
        "model_label": "TabM" if result.get("model_kind", "tabm") != "arrival-net"
                       else f"Model estimate ({result.get('point_rule')} rule)",
    }
    return data


def write_histogram(study_dir: Path) -> Path:
    html = TEMPLATE.read_text().replace("__DATA__", json.dumps(histogram_data(study_dir)))
    out = study_dir / HISTOGRAM_NAME
    out.write_text(html)
    return out
