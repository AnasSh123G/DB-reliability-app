"""The bake-off's comparison table, rendered from a saved manifest."""

from __future__ import annotations


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "–"
    return f"{value:.{digits}f}"


def _pct(value) -> str:
    return "–" if value is None else f"{100 * value:.1f}%"


def _gpu_check_lines(check: dict | None) -> list[str]:
    if not check or "runs" not in check:
        return [] if not check else [f"- GPU check: failed ({check.get('reason')})"]
    gpu, cpu = check["runs"]["cuda"], check["runs"]["cpu"]
    return [
        f"- GPU check ({check['rows']['train']:,} rows, {check['threads']} CPU threads): "
        f"GPU {gpu['seconds']:.0f}s vs CPU {cpu['seconds']:.0f}s ({check['speedup']:.1f}x); "
        f"pinball differs by {100 * check['relative_difference']:.2f}% — "
        f"{'passed' if check['passed'] else 'FAILED'}"
    ]


def render_report(manifest: dict) -> str:
    split = manifest["split"]
    scores = manifest["scores"]
    lines = [
        f"# Delay model bake-off — {manifest['created_at']}",
        "",
        f"- Train: {split['train'][0]} … {split['train'][-1]} ({len(split['train'])} days)",
        f"- Validation: {split['validation'][0]} … {split['validation'][-1]} "
        f"({len(split['validation'])} days) — used to tune and to pick the winner",
        f"- Test: {split['test'][0]} … {split['test'][-1]} ({len(split['test'])} days) — scored once",
        f"- XGBoost GPU: {'yes' if manifest['gpu']['available'] else 'no'} "
        f"({manifest['gpu']['note']}); run alongside CPU libraries: "
        f"{'yes' if manifest['gpu']['parallel'] else 'no'}",
        *_gpu_check_lines(manifest["gpu"].get("check")),
        f"- Winners (by validation): departure **{manifest['winners']['dep']}**, "
        f"arrival **{manifest['winners']['arr']}**",
        "",
        "Figures are minutes, on the test days. Coverage is the share of targets inside the "
        "p25–p75 band (a calibrated band covers ~50%). Only the averages baseline can leave a row "
        "uncovered; *MAE on averages-covered* compares every predictor on those same rows.",
        "",
    ]
    for side, title in (("dep", "Departure"), ("arr", "Arrival")):
        lines += [
            f"## {title}",
            "",
            "| Predictor | Device | Trials | MAE p50 | MAE on averages-covered | Pinball p25 / p50 / p75 "
            "| Band coverage | Rows scored | Val. mean pinball |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for library, per_side in scores["candidates"].items():
            s = per_side[side]
            o = s["overall"]
            cand = manifest["candidates"][library]
            tuning = cand["tuning"]
            trials = f"{tuning['n_trials']}" + (" ⚠ under-tuned" if tuning["under_tuned"] else "")
            mark = " ★" if manifest["winners"][side] == library else ""
            pin = o["pinball"] or {}
            lines.append(
                f"| {library}{mark} | {cand['device']} | {trials} | {_fmt(o['mae_p50'])} "
                f"| {_fmt(s['on_averages_covered']['mae_p50'])} "
                f"| {' / '.join(_fmt(pin.get(k)) for k in ('0.25', '0.5', '0.75'))} "
                f"| {_pct(o['band_coverage'])} | {_pct(o['covered_share'])} "
                f"| {_fmt(cand['sides'][side]['validation_mean_pinball'], 4)} |"
            )
        for name, label in (("averages", "averages (training days)"), ("global", "global median")):
            s = scores["baselines"][name][side]
            o = s["overall"]
            pin = o["pinball"] or {}
            on_cov = o["mae_p50"] if name == "averages" else s["on_averages_covered"]["mae_p50"]
            lines.append(
                f"| *{label}* | – | – | {_fmt(o['mae_p50'])} | {_fmt(on_cov)} "
                f"| {' / '.join(_fmt(pin.get(k)) for k in ('0.25', '0.5', '0.75'))} "
                f"| {_pct(o['band_coverage'])} | {_pct(o['covered_share'])} | – |"
            )
        lines.append("")

        winner = manifest["winners"][side]
        w = scores["candidates"][winner][side]
        avg = scores["baselines"]["averages"][side]
        lines += [f"### {title}: MAE p50 per test day (winner vs averages)", "",
                  "| Date | " + winner + " | averages |", "|---|---|---|"]
        for day, s in w["by_date"].items():
            lines.append(f"| {day} | {_fmt(s['mae_p50'])} | {_fmt(avg['by_date'].get(day, {}).get('mae_p50'))} |")
        lines += ["", f"### {title}: MAE p50 by segment and holiday flag", "",
                  "| Group | " + winner + " | averages |", "|---|---|---|"]
        for key, label in (("by_segment", "segment"), ("by_holiday", "holiday")):
            for value, s in w[key].items():
                lines.append(f"| {label} = {value or '(none)'} | {_fmt(s['mae_p50'])} "
                             f"| {_fmt(avg[key].get(value, {}).get('mae_p50'))} |")
        lines.append("")

    lines += ["## Tuning and fits", ""]
    for library, cand in manifest["candidates"].items():
        t = cand["tuning"]
        lines.append(
            f"- **{library}** ({cand['device']}, {cand['threads']} threads): {t['n_trials']} trials "
            f"+ {t['n_pruned']} pruned in {t['seconds'] / 60:.0f} min; best params `{t['best_params']}`"
        )
        for side in ("dep", "arr"):
            fit = cand["sides"][side]["fit"]
            lines.append(
                f"  - {side}: final fit {fit['fit_seconds'] / 60:.1f} min, rounds {fit['best_rounds']}, "
                f"raw quantile crossing {_pct(cand['sides'][side]['crossed_share_test'])} of test rows"
            )
    for library, error in manifest.get("errors", {}).items():
        lines.append(f"- **{library}** FAILED: {error}")
    lines += ["", f"Total time: {manifest['seconds'] / 60:.0f} min. Versions: {manifest['versions']}.", ""]
    lines += ["Limits: 30 days of history, so no seasonality (winter, leaf fall, timetable "
              "changes); a 6-day test window, so one disrupted day moves the totals — see the "
              "per-day table.", ""]
    return "\n".join(lines)
