"""Export the measured accuracy/precision frontier from the experiment summary."""
from pathlib import Path
import json
import os

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "data/ml/compact_training_20261013"
config = WORK / "tmp/matplotlib"
config.mkdir(parents=True, exist_ok=True)
os.environ["MPLCONFIGDIR"] = str(config)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def main():
    summary = json.loads((WORK / "summary.json").read_text())
    if summary["incomplete_tracks"]:
        raise SystemExit("finish the comparison before publishing the frontier")
    rows = summary["scores"]
    colors = plt.get_cmap("tab10")
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
    for ax, field, title in zip(axes, ("raw", "displayed"), ("Raw model predictions", "Unchanged app display rule")):
        for i, (name, result) in enumerate(rows.items()):
            score = result[field]
            ax.scatter(score["mae_scored"], 100*score["within_5_min"], s=60,
                       marker="*" if name == "reference_median" else "o",
                       color=colors(i), label=name.replace("_", " "))
        ax.set(title=title, xlabel="Mean absolute error (minutes; lower is better)",
               ylabel="Within five minutes (%; higher is better)")
        ax.grid(alpha=.25)
        ax.margins(.12)
    axes[1].legend(fontsize=8, loc="lower right")
    fig.suptitle(f"Compact arrival models — {summary['rows']:,} identical validation rows\n"
                 "September 6–19, 2026 · reused validation, not an independent test", fontsize=12)
    fig.savefig(WORK / "accuracy-precision.svg")
    fig.savefig(WORK / "accuracy-precision.png", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    main()
