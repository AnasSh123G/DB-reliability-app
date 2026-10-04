"""The full-year arrival search from the command line (`dbrel.ml.full_year_search`).

Needs the full-history dataset (`dbrel ml build-full-dataset`) and the PyTorch-ROCm venv.
Every training step runs as a capped job, after a memory check for its track:

    # 1. measure: probes at two row counts, each in its own process; writes
    #    data/ml/full_year/memory_check-<track>.json with the projected peaks
    dbrel ml launch full-year-memcheck-compact --memory 14G --oom-policy continue -- \\
        env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/search_full_year.py \\
        memory-check --track compact

    # 2. search (resumable; the launch gate refuses it without a current check that fits)
    dbrel ml launch full-year-compact --memory 14G -- \\
        env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/search_full_year.py \\
        search --track compact
    dbrel ml launch full-year-large --memory 14G -- \\
        env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/search_full_year.py \\
        search --track large

    # 3. refit both winners and score them once on the sealed test week (records the
    #    test dates as inspected, so a later run needs newer data)
    dbrel ml launch full-year-final --memory 14G -- \\
        env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/search_full_year.py \\
        final --tracks compact,large

    # 4. write a final model as an artifact: compact -> models/v2/, large ->
    #    models/v2-comparison/. Neither moves CURRENT (`dbrel ml promote` does, and
    #    refuses comparison artifacts).
    env PYTHONPATH=src data/build/torch-rocm/venv/bin/python scripts/search_full_year.py \\
        export --track compact --final-dir data/ml/full_year/final/<last test date>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbrel.config import ML_DATA_DIR, ML_MODELS_DIR  # noqa: E402
from dbrel.ml import full_year_search as S  # noqa: E402
from dbrel.ml.split import INSPECTED_NAME, load_inspected  # noqa: E402


def _split_args(args) -> dict:
    return {"test_days": args.test_days, "val_days": args.val_days, "min_train_days": args.min_train_days}


def _setup(args) -> S.SearchSetup:
    return S.setup(args.history, args.work, load_inspected(args.inspected), **_split_args(args))


def _tracks(names: str) -> list[S.Track]:
    unknown = [n for n in names.split(",") if n not in S.TRACKS]
    if unknown:
        raise SystemExit(f"unknown track(s) {unknown}; choose from {sorted(S.TRACKS)}")
    return [S.TRACKS[n] for n in names.split(",")]


def _mae(scores: dict) -> str:
    return "n/a" if scores["mae_scored"] is None else f"{scores['mae_scored']:.3f} min"


def _describe(s: S.SearchSetup) -> None:
    split = s.split
    S.log_line(f"train {split.train[0]}..{split.train[-1]} ({len(split.train)} d), "
               f"validation {split.validation[0]}..{split.validation[-1]} ({len(split.validation)} d), "
               f"test {split.test[0]}..{split.test[-1]} ({len(split.test)} d, sealed until `final`)")


def cmd_search(args) -> int:
    [track] = _tracks(args.track)
    s = _setup(args)
    _describe(s)
    ranking = S.search_track(s, track, device=args.device, models_dir=args.models, max_trials=args.max_trials,
                             max_hours=args.max_hours, replace_inputs=args.rebuild_inputs)
    for t in ranking["trials"]:
        v = t["validation"]
        S.log_line(f"#{t['rank']} {t['id']}: MAE {_mae(v)}, coverage {v['coverage']:.1%}, "
                   f"{t['asset_bytes'] / 1e6:.1f} MB ({t['asset_basis']})")
    for name, ref in ranking["references"].items():
        S.log_line(f"reference {name}: " + (f"MAE {_mae(ref['validation'])}"
                                            if "validation" in ref else ref["missing"]))
    for name, ab in ranking["ablations"].items():
        delta = ab["vs_base_mae"]
        S.log_line(f"ablation {name}: MAE {_mae(ab['validation'])}"
                   + ("" if delta is None else f" ({delta:+.3f} min against the same network with every input)"))
    S.log_line(f"chosen: {ranking['chosen']}; {len(ranking['skipped'])} skipped, "
               f"{len(ranking['not_run'])} not run -> {ranking['experiment']}/ranking.json")
    return 0


def cmd_final(args) -> int:
    s = _setup(args)
    _describe(s)
    result = S.final(s, _tracks(args.tracks), device=args.device, models_dir=args.models,
                     inspected_path=args.inspected, replace_inputs=args.rebuild_inputs)
    for name, scores in result["scores"].items():
        o = scores["overall"]
        S.log_line(f"test {name}: MAE {_mae(o)}, within 3 {o['within_3_min']:.1%}, "
                   f"coverage {o['coverage']:.1%}")
    S.log_line(f"test dates {result['test_dates'][0]}..{result['test_dates'][-1]} recorded as inspected")
    return 0


def cmd_export(args) -> int:
    [track] = _tracks(args.track)
    artifact = S.export_final(args.final_dir, track, models_dir=args.models)
    print(json.dumps({"artifact": str(artifact), "role": track.role}))
    return 0


def cmd_memory_check(args) -> int:
    [track] = _tracks(args.track)
    sizes = tuple(int(n) for n in args.rows.split(","))
    check = S.memory_check(args.history, args.work, track, inspected=load_inspected(args.inspected),
                           split_args=_split_args(args), device=args.device, models_dir=args.models, sizes=sizes)
    for label, key in (("search", "projected_peak"), ("final refit", "projected_final_peak")):
        peak = check[key]
        S.log_line(f"{label}: " + ("a probe was killed at the cap" if peak is None
                                   else f"projected peak {peak / 2**30:.2f} GiB"))
    S.log_line(f"-> {S.memory_check_path(args.work, track.name)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--history", type=Path, default=ML_DATA_DIR / "arrival_full")
    common.add_argument("--work", type=Path, default=ML_DATA_DIR / "full_year")
    common.add_argument("--inspected", type=Path, default=ML_DATA_DIR / INSPECTED_NAME)
    common.add_argument("--models", type=Path, default=ML_MODELS_DIR)
    common.add_argument("--device", default="cuda")
    common.add_argument("--test-days", type=int, default=7)
    common.add_argument("--val-days", type=int, default=28)
    common.add_argument("--min-train-days", type=int, default=90)
    common.add_argument("--rebuild-inputs", action="store_true",
                        help="rebuild prepared inputs whose key changed instead of refusing")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", parents=[common], help="run or resume one track")
    p.add_argument("--track", required=True, choices=sorted(S.TRACKS))
    p.add_argument("--max-trials", type=int)
    p.add_argument("--max-hours", type=float)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("final", parents=[common], help="refit the winners, score the test week once")
    p.add_argument("--tracks", default="compact,large")
    p.set_defaults(func=cmd_final)

    p = sub.add_parser("export", parents=[common], help="write a final model as a role-tagged artifact")
    p.add_argument("--track", required=True, choices=sorted(S.TRACKS))
    p.add_argument("--final-dir", type=Path, required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("memory-check", parents=[common], help="probe a track's peak memory")
    p.add_argument("--track", required=True, choices=sorted(S.TRACKS))
    p.add_argument("--rows", default=",".join(str(n) for n in S.PROBE_ROWS),
                   help="training-row counts to probe, comma-separated")
    p.set_defaults(func=cmd_memory_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
