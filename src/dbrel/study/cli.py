"""`dbrel study ...`: one-off checks against fresh, live data.

Registered on the main app by `dbrel.cli`. Nothing here imports numpy or the model library at
module level, matching `dbrel.ml.cli`'s own reasoning: `dbrel --help` keeps working on a
machine without the `ml` extra.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer

study_app = typer.Typer(add_completion=False, help="One-off checks against fresh, live data.")


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


@study_app.command("live-accuracy")
def live_accuracy_cmd(
    sample_size: int = typer.Option(3000, help="stations to sample"),
    seed: int = typer.Option(None, help="random seed for the sample (default: unseeded)"),
    allow_uncapped: bool = typer.Option(False, help="run outside a `dbrel ml launch` unit "
                                                     "(no memory cap -- fine for a small sample, "
                                                     "not recommended for the full 3,000)"),
    studies_dir: Path = typer.Option(None, help="where to write the report (default: "
                                                "data/ml/studies)"),
) -> None:
    """Sample stations, fetch their live data from the DB Timetables API, and score TabM and
    the stored averages against what already happened -- one live request per station, up to
    `--sample-size` in total. Makes real network requests: run it through `dbrel ml launch`
    (e.g. `dbrel ml launch live-accuracy -- dbrel study live-accuracy`), which keeps it off this
    terminal's process tree and caps its memory, rather than running it here directly, unless
    `--sample-size` is small enough that `--allow-uncapped` is a reasonable shortcut.
    """
    from dbrel.config import DATA_DIR, ML_MODELS_DIR
    from dbrel.db import session_scope
    from dbrel.ingest.client import MissingCredentials, TimetablesClient
    from dbrel.ml.memory import inside_launched_unit
    from dbrel.ml.predict import NoModelError
    from dbrel.study.live_accuracy import run_study, save_report

    if not (inside_launched_unit() or allow_uncapped):
        _fail("not running under `dbrel ml launch` (no memory cap, and it would die with this "
              "terminal) -- start it with `dbrel ml launch live-accuracy -- dbrel study "
              "live-accuracy`, or pass --allow-uncapped for a small sample")

    client = TimetablesClient()
    try:
        client.require_credentials()
    except MissingCredentials as exc:
        _fail(str(exc))

    with session_scope() as session:
        try:
            result = run_study(
                session, client, ML_MODELS_DIR, sample_size=sample_size, seed=seed,
                progress=typer.echo,
            )
        except NoModelError as exc:
            _fail(str(exc))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = save_report(result, studies_dir or (DATA_DIR / "ml" / "studies"), stamp=stamp)
    typer.echo(f"report: {out_dir / 'report.md'}")
