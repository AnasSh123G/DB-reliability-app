"""The live accuracy study: sampling, realized-side filtering, route resolution, scoring."""

from __future__ import annotations

from datetime import datetime

import pytest

from dbrel.config import TZ
from dbrel.direction import direction_by_train_id
from dbrel.ingest.parser import TtEvent, TtStop
from dbrel.ml.predict import DelayModel
from dbrel.models import Station, StopDelayAverage, Train, TrainStopDelayAverage, TripStop
from dbrel.study.live_accuracy import (
    Comparison,
    RealizedSide,
    StudyResult,
    realized_sides,
    resolve_call,
    run_study,
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


# --------------------------------------------------------------------------------------
# 2 & 3. Resolving a realized side and scoring it
# --------------------------------------------------------------------------------------


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


# --------------------------------------------------------------------------------------
# 4. Scoring across many comparisons
# --------------------------------------------------------------------------------------


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


# --------------------------------------------------------------------------------------
# The CLI command
# --------------------------------------------------------------------------------------


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
