"""`dbrel ml ...`: build the dataset, run the bake-off, report, predict.

Registered on the main app by `dbrel.cli`. Nothing here imports numpy or a
model library at module level -- each command imports what it needs -- so
`dbrel --help` keeps working on a machine without the `ml` extra.
"""

from __future__ import annotations

from datetime import date as date_cls
from pathlib import Path

import typer

ml_app = typer.Typer(add_completion=False, help="Offline delay model: dataset, bake-off, prediction.")


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _stamped_echo(message: str) -> None:
    from datetime import datetime

    typer.echo(f"[{datetime.now():%H:%M:%S}] {message}")


@ml_app.command("build-dataset")
def build_dataset_cmd(
    out: Path = typer.Option(None, help="output directory (default: data/ml)"),
    arrival_only: bool = typer.Option(False, help="write the separate arrival-only experiment view "
                                                  "(arrival_dataset.npz); dataset.npz is untouched"),
) -> None:
    """Read every trustworthy observed stop into data/ml/dataset.npz (~15-30 min nationally)."""
    from dbrel.config import DB_PATH, ML_DATA_DIR
    from dbrel.db import session_scope
    from dbrel.ml.dataset import build_arrival_dataset, build_dataset
    from dbrel.ml.features import all_directions

    out = out or ML_DATA_DIR
    typer.echo("resolving directions ...")
    with session_scope() as session:
        directions = all_directions(session)
    typer.echo(f"  {len(directions):,} trains")

    last = [0]

    def progress(n: int) -> None:
        if n - last[0] >= 1_000_000:
            last[0] = n
            typer.echo(f"  {n:,} rows read")

    meta = (build_arrival_dataset if arrival_only else build_dataset)(
        DB_PATH, out, directions, progress=progress)
    typer.echo(
        f"dataset: {meta['n_rows']:,} rows ({meta['n_dep_targets']:,} departure / "
        f"{meta['n_arr_targets']:,} arrival targets) over {meta['dates'][0]} .. {meta['dates'][-1]}, "
        f"built in {meta['build_seconds'] / 60:.1f} min -> {out}"
    )


@ml_app.command("bakeoff")
def bakeoff_cmd(
    libraries: str = typer.Option("lightgbm,catboost,xgboost", help="comma-separated candidates"),
    time_budget: float = typer.Option(60.0, help="tuning minutes per library"),
    trials: int = typer.Option(50, help="max Optuna trials per library"),
    parallel_gpu: bool = typer.Option(True, "--parallel-gpu/--no-parallel-gpu",
                                      help="run GPU XGBoost alongside the CPU libraries"),
    keep_candidates: bool = typer.Option(False, help="also save the losing candidates' models"),
    force: bool = typer.Option(False, help="train even if the database changed since build-dataset"),
    cpu_threads: int = typer.Option(None, help="threads for the CPU libraries (default: all, or 14 "
                                                "while GPU XGBoost runs alongside)"),
    resume: bool = typer.Option(False, help="continue saved work in <dataset-dir>/runs/: finished "
                                             "libraries, tuning trials and sides are not redone"),
    allow_uncapped: bool = typer.Option(False, help="run outside a `dbrel ml launch` unit "
                                                     "(no memory cap -- not for full runs)"),
    dataset_dir: Path = typer.Option(None, help="dataset directory (default: data/ml)"),
) -> None:
    """Tune and fit LightGBM, CatBoost and XGBoost; score them against the baselines.

    Start it with `dbrel ml launch bakeoff -- dbrel ml bakeoff ...` so it runs capped and
    outside the editor and terminal.
    """
    from dbrel.config import DB_PATH, ML_DATA_DIR, ML_MODELS_DIR
    from dbrel.ml.bakeoff import BakeoffConfig, run_bakeoff
    from dbrel.ml.candidates import LIBRARIES
    from dbrel.ml.dataset import META_NAME, dataset_is_stale, load_dataset
    from dbrel.ml.memory import inside_launched_unit

    if not (inside_launched_unit() or allow_uncapped):
        _fail("not running under `dbrel ml launch` (no memory cap, and it would die with this "
              "terminal) -- start it with `dbrel ml launch bakeoff -- dbrel ml bakeoff ...`, "
              "or pass --allow-uncapped for a small experiment")
    dataset_dir = dataset_dir or ML_DATA_DIR
    if not (dataset_dir / META_NAME).exists():
        _fail(f"no dataset in {dataset_dir} -- run `dbrel ml build-dataset` first")
    chosen = tuple(lib.strip() for lib in libraries.split(",") if lib.strip())
    unknown = [lib for lib in chosen if lib not in LIBRARIES]
    if unknown:
        _fail(f"unknown libraries: {unknown}; choose from {list(LIBRARIES)}")

    ds = load_dataset(dataset_dir)
    if dataset_is_stale(ds.meta, DB_PATH) and not force:
        _fail("the database changed since the dataset was built -- rerun `dbrel ml build-dataset`, "
              "or pass --force to train on the existing dataset anyway")
    runs = dataset_dir / "runs"
    cfg = BakeoffConfig(libraries=chosen, time_budget_s=time_budget * 60, max_trials=trials,
                        parallel_gpu=parallel_gpu, keep_candidates=keep_candidates,
                        cpu_threads=cpu_threads, cache_dir=runs, resume=resume,
                        drop_raw_features=True)
    if "xgboost" in chosen and not (runs / "xgboost" / "done").exists():
        # The GPU is only used once the correctness check has passed on this dataset.
        from dbrel.ml.candidates import xgboost_has_gpu
        from dbrel.ml.gpu_check import load_gpu_check, run_gpu_check

        gpu_ok, note = xgboost_has_gpu()
        check = load_gpu_check(dataset_dir)
        if gpu_ok and (check is None or check.get("checked_at", 0) < ds.meta["built_at"]):
            typer.echo("running the GPU correctness check first ...")
            check = run_gpu_check(ds, dataset_dir, threads=cfg.total_threads, log=typer.echo)
        cfg.gpu_check = check
        if gpu_ok and not (check and check.get("passed")):
            cfg.gpu = (False, f"GPU check failed: {check.get('reason') if check else 'not run'}")
        else:
            cfg.gpu = (gpu_ok, note)
    from dbrel.ml.bakeoff import SavedWorkExists
    from dbrel.ml.checkpoint import CheckpointMismatch

    try:
        artifact = run_bakeoff(ds, ML_MODELS_DIR, cfg, log=_stamped_echo)
    except (CheckpointMismatch, SavedWorkExists) as exc:
        _fail(str(exc))
    typer.echo((artifact / "report.md").read_text())


@ml_app.command("gpu-check")
def gpu_check_cmd(
    dataset_dir: Path = typer.Option(None, help="dataset directory (default: data/ml)"),
    threads: int = typer.Option(None, help="CPU threads for both fits (default: all)"),
) -> None:
    """Fit the same XGBoost model on GPU and CPU; they must agree before the GPU is used."""
    import os

    from dbrel.config import ML_DATA_DIR
    from dbrel.ml.dataset import META_NAME, load_dataset
    from dbrel.ml.gpu_check import run_gpu_check

    dataset_dir = dataset_dir or ML_DATA_DIR
    if not (dataset_dir / META_NAME).exists():
        _fail(f"no dataset in {dataset_dir} -- run `dbrel ml build-dataset` first")
    result = run_gpu_check(load_dataset(dataset_dir), dataset_dir, threads=threads or os.cpu_count() or 4,
                           log=typer.echo)
    if not result["passed"]:
        _fail(f"GPU check failed: {result['reason']}")
    typer.echo(f"GPU check passed: {result['speedup']:.1f}x faster than CPU, pinball within "
               f"{100 * result['relative_difference']:.2f}%")


@ml_app.command("report")
def report_cmd(artifact: Path = typer.Argument(None, help="artifact directory (default: CURRENT)")) -> None:
    """Reprint a bake-off's comparison table from its saved manifest."""
    import json

    from dbrel.config import ML_MODELS_DIR
    from dbrel.ml.bakeoff import MANIFEST_NAME, current_artifact
    from dbrel.ml.report import render_report

    artifact = artifact or current_artifact(ML_MODELS_DIR)
    if artifact is None or not (artifact / MANIFEST_NAME).exists():
        _fail(f"no bake-off artifact found under {ML_MODELS_DIR} -- run `dbrel ml bakeoff` first")
    typer.echo(render_report(json.loads((artifact / MANIFEST_NAME).read_text())))


def _resolve_train(session, term: str):
    from sqlalchemy import select

    from dbrel.identity import search_norm
    from dbrel.models import SearchAlias, Train

    norm = search_norm(term)
    ids = session.execute(
        select(SearchAlias.train_id)
        .where(SearchAlias.alias_norm == norm, SearchAlias.train_id.isnot(None))
        .order_by(SearchAlias.weight.desc())
    ).scalars().all()
    if not ids:
        ids = session.execute(select(Train.id).where(Train.search_name == norm)).scalars().all()
    return [session.get(Train, i) for i in dict.fromkeys(ids)]


def _resolve_station(session, term: str):
    from sqlalchemy import func, select

    from dbrel.identity import search_norm
    from dbrel.models import SearchAlias, Station

    exact = session.execute(select(Station).where(func.lower(Station.name) == term.lower())).scalars().all()
    if exact:
        return exact
    ids = session.execute(
        select(SearchAlias.station_id)
        .where(SearchAlias.alias_norm == search_norm(term), SearchAlias.station_id.isnot(None))
    ).scalars().all()
    if ids:
        return [session.get(Station, i) for i in dict.fromkeys(ids)]
    return session.execute(
        select(Station).where(Station.name.ilike(f"%{term}%"), Station.tracked == 1).limit(10)
    ).scalars().all()


def _hhmm(sec: int | None) -> str:
    if sec is None:
        return "  –  "
    sec %= 86400
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"


def _band(q) -> str:
    if q is None:
        return "–"
    return f"{q[1]:+.1f} min ({q[0]:+.1f} … {q[2]:+.1f})"


@ml_app.command("predict-delay")
def predict_delay_cmd(
    station: str = typer.Option(..., help="station name"),
    date: str = typer.Option(..., help="service date, YYYY-MM-DD"),
    train: str = typer.Option(None, help='a train, e.g. "ICE 599"'),
    line: str = typer.Option(None, help='a line, e.g. "RE22" (with --time)'),
    time: str = typer.Option(None, help="HH:MM; with --line, the calls nearest this time"),
    direction: int = typer.Option(None, help="with --line: 0 or 1 (default: both)"),
    count: int = typer.Option(3, help="with --line: calls per direction"),
) -> None:
    """Predict delay (an estimate and p25-p75) from the model and timetable alone.

    A v2 TabM artifact predicts both sides, its estimate being the median; an
    arrival-net artifact predicts arrivals only, its estimate being the centre
    of the most likely 6-minute window (not the median).
    """
    from dbrel.config import ML_MODELS_DIR
    from dbrel.db import session_scope
    from dbrel.direction import line_trains_by_direction
    from dbrel.ml.predict import DelayModel, NoModelError
    from dbrel.ml.timetable import calls_at

    if (train is None) == (line is None):
        _fail("give exactly one of --train or --line")
    try:
        day = date_cls.fromisoformat(date)
    except ValueError:
        _fail(f"--date must be YYYY-MM-DD, got {date!r}")
    try:
        model = DelayModel.current(ML_MODELS_DIR)
    except NoModelError as exc:
        _fail(str(exc))

    with session_scope() as session:
        stations = _resolve_station(session, station)
        if not stations:
            _fail(f"no station matches {station!r}")
        if len(stations) > 1:
            _fail("several stations match: " + ", ".join(s.name for s in stations[:10]))
        st = stations[0]

        calls = []
        if train is not None:
            trains = _resolve_train(session, train)
            if not trains:
                _fail(f"no train matches {train!r}")
            for t in trains:
                calls.extend(calls_at(session, t, st.id, day))
            if not calls:
                _fail(f"{train} has no scheduled call at {st.name}")
        else:
            from dbrel.cli import _resolve_line_candidates

            if time is None:
                _fail("--line needs --time")
            target = int(time[:2]) * 3600 + int(time[3:5]) * 60
            for line_id in _resolve_line_candidates(session, line):
                _, halves = line_trains_by_direction(session, line_id, limit=100_000)
                for d, half in enumerate(halves):
                    if direction is not None and d != direction:
                        continue
                    found = [c for t in half for c in calls_at(session, t, st.id, day)]

                    def gap(c):
                        sec = c.planned_departure_sec if c.planned_departure_sec is not None else c.planned_arrival_sec
                        diff = abs((sec % 86400) - target)
                        return min(diff, 86400 - diff)

                    calls.extend(sorted(found, key=gap)[:count])
            if not calls:
                _fail(f"{line} has no scheduled call at {st.name}")

        predictions = model.predict_calls(session, calls)
        typer.echo(f"{st.name}, {day.isoformat()} — model {model.artifact.name} "
                   f"(historical pattern, not a live forecast)")
        if model.arrival is not None:
            typer.echo("  arrival only: estimate = centre of the most likely 6-minute window "
                       "(not the median), band p25 … p75; departures are not predicted")
        for p in sorted(predictions, key=lambda p: (p.call.planned_departure_sec or p.call.planned_arrival_sec or 0)):
            c = p.call
            typer.echo(
                f"  {c.train.display_name or c.train.id:<12} arr {_hhmm(c.planned_arrival_sec)} {_band(p.arr):<28}"
                f" dep {_hhmm(c.planned_departure_sec)} {_band(p.dep)}"
            )


@ml_app.command("launch")
def launch_cmd(
    name: str = typer.Argument(..., help="job name, e.g. bakeoff (unit dbrel-ml-<name>.service)"),
    command: list[str] = typer.Argument(..., help="after `--`: the command; join steps with `::`"),
    memory: str = typer.Option("18G", help="hard memory cap (MemoryMax); swap is disabled"),
    oom_policy: str = typer.Option("stop", help="stop: end the unit at the cap; continue: only "
                                                 "the offending process dies (memory-check)"),
    ignore_memory_check: bool = typer.Option(False, help="launch a bake-off even if the memory "
                                                          "check is missing or says it won't fit"),
) -> None:
    """Start a job as a capped systemd user service, outside any editor or terminal."""
    from pathlib import Path

    from dbrel.config import ML_DATA_DIR
    from dbrel.ml import memcheck
    from dbrel.ml.launch import LaunchError, launch

    gate = None if ignore_memory_check else (
        lambda steps, cap: memcheck.gate(steps, cap, ML_DATA_DIR.resolve()))
    note = "memory check IGNORED (--ignore-memory-check)" if ignore_memory_check else None
    try:
        unit, log = launch(name, command, workdir=Path.cwd(), logs=ML_DATA_DIR.resolve() / "logs",
                           memory=memory, oom_policy=oom_policy, gate=gate, note=note)
    except LaunchError as exc:
        _fail(str(exc))
    typer.echo(f"started {unit} (MemoryMax={memory}, no swap)")
    typer.echo(f"  log:    {log}")
    typer.echo(f"  status: dbrel ml status {name}")
    typer.echo(f"  stop:   systemctl --user stop {unit}")


@ml_app.command("status")
def status_cmd(name: str = typer.Argument(..., help="job name given to `dbrel ml launch`")) -> None:
    """Whether a launched job is running, and how it ended: exit reason, memory peak, log."""
    from dbrel.config import ML_DATA_DIR
    from dbrel.ml.launch import LaunchError, describe
    from dbrel.ml.memory import GIB

    try:
        info = describe(name, ML_DATA_DIR.resolve() / "logs")
    except LaunchError as exc:
        _fail(str(exc))
    state = info["state"]
    typer.echo(f"{info['unit']}: {info['verdict']}")
    if state.get("LoadState") == "loaded":
        typer.echo(f"  state:   {state.get('ActiveState')} ({state.get('SubState')}), "
                   f"result={state.get('Result')}, exit status {state.get('ExecMainStatus')}")
        typer.echo(f"  started: {state.get('ExecMainStartTimestamp') or '-'}")
        typer.echo(f"  ended:   {state.get('ExecMainExitTimestamp') or '-'}")
        peak = state.get("MemoryPeak", "")
        if peak.isdigit():
            typer.echo(f"  memory peak: {int(peak) / GIB:.2f} GiB (cap {state.get('MemoryMax')})")
        typer.echo(f"  cgroup:  {state.get('ControlGroup') or '-'}")
    typer.echo(f"  log:     {info['log'] or '(none)'}")
    if info["journal"]:
        typer.echo("  journal:")
        for line in info["journal"].splitlines():
            typer.echo(f"    {line}")


@ml_app.command("memory-check")
def memory_check_cmd(
    libraries: str = typer.Option("lightgbm,catboost,xgboost", help="comma-separated candidates"),
    threads: int = typer.Option(14, help="threads per probe fit (use what the bake-off will use)"),
    allow_uncapped: bool = typer.Option(False, help="run outside a `dbrel ml launch` unit"),
    dataset_dir: Path = typer.Option(None, help="dataset directory (default: data/ml)"),
) -> None:
    """Probe each library's peak memory and project it to the full bake-off (minutes, not hours).

    Start it with `dbrel ml launch memcheck --oom-policy continue -- dbrel ml memory-check ...`.
    """
    from dbrel.config import ML_DATA_DIR
    from dbrel.ml.candidates import LIBRARIES
    from dbrel.ml.memcheck import RESULT_NAME, run_memory_check
    from dbrel.ml.memory import inside_launched_unit

    if not (inside_launched_unit() or allow_uncapped):
        _fail("not running under `dbrel ml launch` -- start it with `dbrel ml launch memcheck "
              "--oom-policy continue -- dbrel ml memory-check ...`, or pass --allow-uncapped")
    dataset_dir = dataset_dir or ML_DATA_DIR
    chosen = tuple(lib.strip() for lib in libraries.split(",") if lib.strip())
    unknown = [lib for lib in chosen if lib not in LIBRARIES]
    if unknown:
        _fail(f"unknown libraries: {unknown}; choose from {list(LIBRARIES)}")
    run_memory_check(dataset_dir, chosen, threads=threads, log=_stamped_echo)
    typer.echo(f"written: {dataset_dir / RESULT_NAME}")
