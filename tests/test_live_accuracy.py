"""The live accuracy study: sampling, realized-side filtering, route resolution, scoring."""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from dbrel.config import TZ
from dbrel.direction import direction_by_train_id
from dbrel.ingest.changes_poller import GRACE_S
from dbrel.ingest.parser import TtEvent, TtStop
from dbrel.ml.predict import DelayModel, NoModelError
from dbrel.models import Station, StopDelayAverage, Train, TrainStopDelayAverage, TripStop
from dbrel.study.live_accuracy import (
    REPORT_NAME,
    RESULT_NAME,
    Comparison,
    RealizedSide,
    StudyResult,
    fetch_station,
    realized_sides,
    render_report,
    resolve_call,
    run_study,
    sample_stations,
    save_report,
    score_comparisons,
    score_stops,
)
from dbrel.web.queries import _sched_time_key, _train_trip_start_time, resolve_day_type

from ml_fixture import PATTERNS, START, utc_of
from test_ml_arrival_deploy import FakeQuantileSession, _quantile_manifest, arrival_artifact  # noqa: F401
from test_ml_bakeoff import env  # noqa: F401 - the fixture database and dataset
from test_ml_nn import tabm_artifact  # noqa: F401 - a loadable DelayModel artifact (fake ONNX)

# Train 70's stop at Dortmund Hbf (station 2), from ml_fixture's PATTERNS: (station, arr, dep).
STATION_ID, ARR_SEC, DEP_SEC = PATTERNS[70][1]
SERVICE_DATE = START.isoformat()


def _stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=TZ).strftime("%y%m%d%H%M")


def _event(planned_sec: int, *, delay_s: int | None = 180, changed: bool = True,
          cancelled: bool = False) -> TtEvent:
    planned_ts = utc_of(SERVICE_DATE, planned_sec)
    changed_ts = (planned_ts + delay_s) if changed and delay_s is not None else None
    return TtEvent(
        planned_ts=planned_ts, changed_ts=changed_ts, planned_platform=None,
        changed_platform=None, planned_status="p", changed_status="c" if cancelled else None,
        planned_path=None, changed_path=None, line=None, cancelled_at=None, hidden=False,
        wings=None, transition=None,
    )


def _stop(*, category="RE", number="10070", arr: TtEvent | None = None,
         dep: TtEvent | None = None) -> TtStop:
    stamp = SERVICE_DATE.replace("-", "")[2:] + "0800"
    return TtStop(stop_id="s-1", eva=1234, daily_trip_id="t-1", start_stamp=stamp, stop_index=1,
                 category=category, number=number, owner="DB", trip_type="p", filter_flags=None,
                 label=None, arrival=arr, departure=dep)


def _realized_side(station: Station, *, delay_s: int = 180) -> RealizedSide:
    event = _event(DEP_SEC, delay_s=delay_s)
    return RealizedSide(station=station, stop=_stop(dep=event), side="dep", event=event,
                        real_delay_min=event.delay_s / 60)


# --------------------------------------------------------------------------------------
# 1. Sampling and realized-side filtering
# --------------------------------------------------------------------------------------


def test_sample_stations_is_distinct_and_sized(env):  # noqa: F811
    import dbrel.db as dbmod

    with dbmod.session_scope() as s:
        for sid in (1, 2, 3):
            s.get(Station, sid).eva = 8000000 + sid
        s.flush()
        chosen = sample_stations(s, 2, seed=0)
        assert len({st.id for st in chosen}) == len(chosen) == 2
        assert all(st.eva is not None for st in chosen)
        # Asking for more than exist returns every eligible station, not an error.
        assert len(sample_stations(s, 100, seed=0)) == 3


def test_realized_sides_keeps_only_genuine_past_outcomes():
    now = utc_of(SERVICE_DATE, DEP_SEC) + 3600
    fully_realized = _stop(dep=_event(DEP_SEC))
    only_arrival_realized = _stop(arr=_event(ARR_SEC), dep=_event(DEP_SEC, changed=False))
    planned_only = _stop(arr=_event(ARR_SEC, changed=False))
    near_future_estimate = _stop(dep=TtEvent(
        planned_ts=now + 1000, changed_ts=now + 5, planned_platform=None, changed_platform=None,
        planned_status="p", changed_status=None, planned_path=None, changed_path=None, line=None,
        cancelled_at=None, hidden=False, wings=None, transition=None))
    cancelled = _stop(dep=_event(DEP_SEC, cancelled=True))

    station = Station(id=STATION_ID, natural_key="x", name="x", lat=0, lon=0, tracked=1)
    out = realized_sides(
        station, [fully_realized, only_arrival_realized, planned_only, near_future_estimate,
                  cancelled], now)
    by_stop = {}
    for r in out:
        by_stop.setdefault(id(r.stop), []).append(r.side)

    assert by_stop.get(id(fully_realized)) == ["dep"]
    assert by_stop.get(id(only_arrival_realized)) == ["arr"]
    assert id(planned_only) not in by_stop
    assert id(near_future_estimate) not in by_stop
    assert id(cancelled) not in by_stop


def test_realized_sides_respects_the_grace_period():
    """A changed time just under GRACE_S in the past is still an estimate, not a fact."""
    assert GRACE_S < 180  # sanity: the gap below must exceed the real grace period
    now = utc_of(SERVICE_DATE, DEP_SEC) + 180
    just_inside = _stop(dep=_event(DEP_SEC, delay_s=0))  # changed_ts = now - 180
    station = Station(id=STATION_ID, natural_key="x", name="x", lat=0, lon=0, tracked=1)
    assert realized_sides(station, [just_inside], now)


def test_fetch_station_skips_on_error_or_empty():
    class FakeResp:
        def __init__(self, status, body=b""):
            self.status, self.body, self.ok = status, body, status == 200

    class FakeClient:
        def __init__(self, resp):
            self.resp = resp

        def changes(self, eva):
            return self.resp

        def plan(self, eva, date, hour):
            return self.resp

    station = Station(id=1, natural_key="x", name="x", lat=0, lon=0, tracked=1, eva=8000041)
    now = utc_of(SERVICE_DATE, DEP_SEC) + 3600

    bad = fetch_station(FakeClient(FakeResp(500)), station, now)
    assert bad.skipped and "http 500" in bad.skip_reason

    empty = fetch_station(FakeClient(FakeResp(200, b"<timetable/>")), station, now)
    assert empty.skipped and empty.skip_reason == "no realized stops"


def test_fetch_station_takes_the_train_from_plan_and_the_real_time_from_fchg():
    """/fchg alone has no train number or planned time for an ordinary run -- the bug the
    first real run hit, which kept only special trains. Both halves must be joined by stop id."""
    now = utc_of(SERVICE_DATE, DEP_SEC) + 3600
    client = FakeTimetables(eva=8000002)
    station = Station(id=STATION_ID, natural_key="x", name="x", lat=0, lon=0, tracked=1,
                      eva=8000002)
    result = fetch_station(client, station, now)
    assert not result.skipped
    [side] = result.realized
    assert (side.stop.category, side.stop.number, side.side) == ("RE", "10070", "dep")
    assert side.real_delay_min == pytest.approx(3.0)


# --------------------------------------------------------------------------------------
# 2 & 3. Resolving a realized side and scoring it
# --------------------------------------------------------------------------------------


def test_resolve_call_matches_a_known_train(env):  # noqa: F811
    import dbrel.db as dbmod

    with dbmod.session_scope() as s:
        side = _realized_side(s.get(Station, STATION_ID))
        scored = resolve_call(s, side)
        assert scored is not None
        assert scored.train.id == 70
        assert scored.call.stop_rank == 1 and scored.call.n_stops == len(PATTERNS[70])


def test_resolve_call_returns_none_for_an_unknown_train(env):  # noqa: F811
    import dbrel.db as dbmod

    with dbmod.session_scope() as s:
        event = _event(DEP_SEC)
        side = RealizedSide(
            station=s.get(Station, STATION_ID),
            stop=_stop(number="99999", dep=event), side="dep", event=event,
            real_delay_min=event.delay_s / 60,
        )
        assert resolve_call(s, side) is None


def test_score_stops_matches_the_deployed_average_and_model(env, tabm_artifact):  # noqa: F811
    import dbrel.db as dbmod

    with dbmod.session_scope() as s:
        train = s.get(Train, 70)
        direction = direction_by_train_id(s, train.line_id).get(train.id)
        s.add(StopDelayAverage(
            station_id=STATION_ID, line_id=train.line_id, direction=direction,
            trip_start_time=_train_trip_start_time(train),
            sched_time=_sched_time_key(ARR_SEC, DEP_SEC), is_holiday=0,
            n_dep=10, mean_dep_delay_s=240.0, n_arr=10, mean_arr_delay_s=180.0, built_at=0,
        ))
        s.flush()

        now = utc_of(SERVICE_DATE, DEP_SEC) + 3600
        assert resolve_day_type(s, START.isoformat(), station=s.get(Station, STATION_ID)) is not None

        side = _realized_side(s.get(Station, STATION_ID))
        scored = resolve_call(s, side)
        model = DelayModel.current(tabm_artifact.parent)
        [comparison] = score_stops(s, model, [scored], now=now)

        assert comparison.average == (4.0, 4.0, 4.0)  # mean_dep_delay_s / 60, lo=hi=p50
        assert comparison.tabm is not None  # FakeSession's deterministic formula produced a value
        assert comparison.real_delay_min == pytest.approx(3.0)


def test_score_stops_reports_uncovered_average_but_still_scores_tabm(env, tabm_artifact):  # noqa: F811
    import dbrel.db as dbmod

    with dbmod.session_scope() as s:
        now = utc_of(SERVICE_DATE, DEP_SEC) + 3600
        side = _realized_side(s.get(Station, STATION_ID))
        scored = resolve_call(s, side)
        model = DelayModel.current(tabm_artifact.parent)
        [comparison] = score_stops(s, model, [scored], now=now)  # no StopDelayAverage row exists
        assert comparison.average is None
        assert comparison.tabm is not None


# --------------------------------------------------------------------------------------
# 4. Scoring across many comparisons
# --------------------------------------------------------------------------------------


def test_score_comparisons_compares_on_the_same_stops():
    rows = [
        Comparison(side="dep", segment="RV", is_holiday=0, service_date="2026-09-01",
                  real_delay_min=2.0, tabm=(1.0, 2.0, 3.0), average=(2.0, 2.0, 2.0)),
        Comparison(side="dep", segment="RV", is_holiday=0, service_date="2026-09-02",
                  real_delay_min=4.0, tabm=(2.0, 4.0, 6.0), average=None),
        Comparison(side="arr", segment="FV", is_holiday=1, service_date="2026-09-01",
                  real_delay_min=1.0, tabm=(0.0, 1.0, 2.0), average=(1.0, 1.0, 1.0)),
    ]
    out = score_comparisons(rows)
    dep = out["dep"]
    assert dep["n"] == 2 and dep["n_compared"] == 1 and dep["n_no_average"] == 1
    assert dep["tabm"]["minutes_off"] == 0.0 and dep["tabm"]["within_1"] == 1.0
    assert (dep["tabm_closer"], dep["average_closer"], dep["tie"]) == (0, 0, 1)
    assert dep["tabm_on_no_average"]["minutes_off"] == 0.0
    assert out["arr"]["n_compared"] == 1


def test_score_comparisons_handles_a_side_with_nothing_scored():
    out = score_comparisons([Comparison(side="dep", segment="RV", is_holiday=0,
                                        service_date="2026-09-01", real_delay_min=1.0,
                                        tabm=(0.0, 1.0, 2.0), average=None)])
    assert out["arr"]["n"] == 0 and out["arr"]["n_compared"] == 0
    assert out["arr"]["tabm"]["minutes_off"] is None


# --------------------------------------------------------------------------------------
# 5 & 6. The whole run, and its read-only guarantee
# --------------------------------------------------------------------------------------

class FakeTimetables:
    """One station answering like IRIS does: /plan has the train and planned time, /fchg only
    the changed time (a pure delta, no <tl>, no pt)."""

    def __init__(self, eva):
        self.eva = eva
        planned = utc_of(SERVICE_DATE, DEP_SEC)
        self.plan_xml = (f'<timetable eva="{eva}"><s id="123-2609010800-2">'
                         f'<tl o="DB" c="RE" n="10070"/><dp pt="{_stamp(planned)}"/>'
                         f'</s></timetable>').encode()
        self.fchg_xml = (f'<timetable eva="{eva}"><s id="123-2609010800-2">'
                         f'<dp ct="{_stamp(planned + 180)}"/></s></timetable>').encode()

    def _resp(self, body):
        class R:
            ok, status = True, 200
        r = R()
        r.body = body
        return r

    def require_credentials(self):
        pass

    def changes(self, eva):
        return self._resp(self.fchg_xml if eva == self.eva else b"<timetable/>")

    def plan(self, eva, date, hour):
        return self._resp(self.plan_xml if eva == self.eva else b"<timetable/>")


def test_run_study_is_read_only(env, tabm_artifact):  # noqa: F811
    import dbrel.db as dbmod

    now = utc_of(SERVICE_DATE, DEP_SEC) + 3600

    with dbmod.session_scope() as s:
        s.get(Station, STATION_ID).eva = 8000002
        s.flush()

    def snapshot():
        with dbmod.session_scope() as s:
            return (
                [(r.trip_id, r.dep_delay_s) for r in s.query(TripStop)],
                [(r.station_id, r.mean_dep_delay_s) for r in s.query(StopDelayAverage)],
                [(r.train_id, r.mean_dep_delay_s) for r in s.query(TrainStopDelayAverage)],
            )

    before = snapshot()
    models_before = sorted(p.name for p in tabm_artifact.parent.rglob("*"))

    with dbmod.session_scope() as s:
        result = run_study(s, FakeTimetables(8000002), tabm_artifact.parent, sample_size=1,
                           seed=0, now=now)

    assert before == snapshot()
    assert models_before == sorted(p.name for p in tabm_artifact.parent.rglob("*"))
    assert result.stations_sampled == 1
    assert result.n_scored["dep"] == 1


def test_run_study_raises_clearly_with_no_trained_model(env):  # noqa: F811
    import dbrel.db as dbmod

    class FakeClient:
        def changes(self, eva):
            raise AssertionError("must not fetch before checking a model exists")

    with dbmod.session_scope() as s:
        with pytest.raises(NoModelError):
            run_study(s, FakeClient(), env / "models", sample_size=1)


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------


def _result(**overrides) -> StudyResult:
    base = dict(
        stations_sampled=10, stations_skipped=2, skip_reasons={"http 500": 1, "no realized stops": 1},
        stops_skipped_no_local_match=1,
        scores=score_comparisons([
            Comparison(side="dep", segment="RV", is_holiday=0, service_date="2026-09-01",
                      real_delay_min=2.0, tabm=(1.0, 2.0, 3.0), average=(2.0, 2.0, 2.0)),
        ]),
        n_scored={"dep": 1, "arr": 0},
        comparisons=[Comparison(side="dep", segment="RV", is_holiday=0,
                                service_date="2026-09-01", real_delay_min=2.0,
                                tabm=(1.0, 2.0, 3.0), average=(2.0, 2.0, 2.0))],
    )
    base.update(overrides)
    return StudyResult(**base)


def test_render_report_includes_every_headline_figure():
    text = render_report(_result(), created_at="20260101T000000Z")
    assert "Stations sampled: 10, of which 2" in text
    assert "1 http 500" in text and "1 no realized stops" in text
    assert "isn't in the local timetable (skipped): 1" in text
    assert "Average minutes off" in text and "Within 3 min" in text
    assert "Closer to the real delay: TabM 0 times, stored average 0 times, same 1 times" in text
    assert "Nothing to compare." in text  # arr had none


def test_a_median_arrival_model_is_labelled_a_median(tmp_path):
    arrival = dict(model_kind="arrival-net", point_rule="median", artifact="/m/v2-comparison/20261001T000000Z",
                   role="comparison")
    text = render_report(_result(**arrival), created_at="20260101T000000Z")
    assert "The model's figure is its median guess" in text and "window rule" not in text
    assert "`/m/v2-comparison/20261001T000000Z` (comparison model: never served on the website)" in text
    out_dir = save_report(_result(**arrival, scores=score_comparisons([
        Comparison(side="arr", segment="RV", is_holiday=0, service_date="2026-09-01",
                   real_delay_min=2.0, tabm=(1.0, 2.0, 3.0), average=(2.0, 2.0, 2.0))]),
        comparisons=[Comparison(side="arr", segment="RV", is_holiday=0, service_date="2026-09-01",
                                real_delay_min=2.0, tabm=(1.0, 2.0, 3.0), average=(2.0, 2.0, 2.0))]),
        tmp_path, stamp="20260101T000000Z")
    assert "Closer to the real delay: Model median" in (out_dir / REPORT_NAME).read_text()
    assert "model_p50" in (out_dir / "comparisons.csv").read_text().splitlines()[0]
    html = (out_dir / "histogram.html").read_text()
    assert '"model_label": "Model median"' in html and '"n": 1' in html

    window = render_report(_result(model_kind="arrival-net", point_rule="window"), created_at="x")
    assert "6-minute window (window rule), not a median" in window


def test_run_study_scores_a_given_artifact_without_reading_current(env, arrival_artifact, tmp_path,  # noqa: F811
                                                                  monkeypatch):
    import onnxruntime

    import dbrel.db as dbmod
    from dbrel.ml.arrival import role_models_dir, save_arrival_artifact
    from dbrel.ml.nn import CURRENT_NAME

    monkeypatch.setattr(onnxruntime, "InferenceSession", FakeQuantileSession)
    models = arrival_artifact.parent
    onnx = tmp_path / "small.onnx"
    onnx.write_bytes(b"x" * 1000)
    comparison = save_arrival_artifact(role_models_dir("comparison", models), _quantile_manifest(arrival_artifact),
                                       {"model.onnx": onnx}, "20261003T000000Z", role="comparison",
                                       served_dir=models)
    (models / CURRENT_NAME).write_text("does-not-exist\n")
    with dbmod.session_scope() as s:
        s.get(Station, STATION_ID).eva = 8000002
        s.flush()
    with dbmod.session_scope() as s:
        result = run_study(s, FakeTimetables(8000002), models, sample_size=1, seed=0,
                           now=utc_of(SERVICE_DATE, DEP_SEC) + 3600, artifact=comparison)
        assert (result.artifact, result.role) == (str(comparison), "comparison")
        assert (result.model_kind, result.point_rule) == ("arrival-net", "median")
        with pytest.raises(NoModelError, match="not a model artifact"):
            run_study(s, FakeTimetables(8000002), models, sample_size=1, artifact=tmp_path)


def test_save_report_writes_both_files_and_refuses_to_overwrite(tmp_path):
    out_dir = save_report(_result(), tmp_path, stamp="20260101T000000Z")
    assert (out_dir / RESULT_NAME).exists() and (out_dir / REPORT_NAME).exists()
    rows = (out_dir / "comparisons.csv").read_text().splitlines()
    assert rows[0].startswith("side,station,train") and len(rows) == 2
    html = (out_dir / "histogram.html").read_text()
    assert "__DATA__" not in html and '"n": 1' in html
    with pytest.raises(FileExistsError):
        save_report(_result(), tmp_path, stamp="20260101T000000Z")


# --------------------------------------------------------------------------------------
# The CLI command
# --------------------------------------------------------------------------------------


def test_cli_help_documents_the_flags():
    from typer.testing import CliRunner

    from dbrel.cli import app

    result = CliRunner().invoke(app, ["study", "live-accuracy", "--help"])
    assert result.exit_code == 0, result.output
    # CI forces colour on, which puts escape codes inside the flag names.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--sample-size" in plain and "--seed" in plain
    assert "--allow-uncapped" in plain


def test_cli_run_against_a_fixture_writes_a_report(env, tabm_artifact, monkeypatch):  # noqa: F811
    import dbrel.config as config
    import dbrel.ingest.client as client_mod
    from typer.testing import CliRunner

    from dbrel.cli import app

    monkeypatch.setattr(config, "ML_MODELS_DIR", tabm_artifact.parent)
    monkeypatch.setattr(client_mod, "TimetablesClient", lambda: FakeTimetables(8000002))

    result = CliRunner().invoke(app, [
        "study", "live-accuracy", "--allow-uncapped", "--sample-size", "1",
        "--studies-dir", str(env / "studies"),
    ])
    assert result.exit_code == 0, result.output
    assert "report:" in result.output
    reports = list((env / "studies").glob("*/report.md"))
    assert len(reports) == 1
