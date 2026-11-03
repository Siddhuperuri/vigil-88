"""Worker restart over HTTP, and the degradation state where every client can see it."""

from __future__ import annotations

from pathlib import Path

from tests.unit.api.accounts_rig import (
    GOOD,
    MakeConfig,
    Rig,
    build_rig,
    make_user,
    set_up,
    signed_in,
)

CAMERA = {"camera_id": "synth", "source": {"kind": "synthetic"}}
# One long interval so the background ladder worker stays out of the way; the tests tick by hand.
SLOW_LADDER = {"degradation.interval_ms": 60_000, "degradation.escalate_after_ms": 1000}


def workers(client) -> dict[str, dict]:  # type: ignore[no-untyped-def,type-arg]
    return {w["name"]: w for w in client.get("/api/v1/system/workers").json()}


def test_workers_are_listed_with_whether_each_can_be_restarted(
    make_config: MakeConfig, tmp_path: Path
) -> None:
    with build_rig(make_config, tmp_path, extra={"cameras": [CAMERA]}) as rig:
        set_up(rig)
        with signed_in(rig, "alice") as a:
            listed = workers(a)
    assert listed["inference"]["restartable"] is False
    assert listed["sampler"]["restartable"] is True
    # A synthetic camera is a paced, replayable source, and a replay cannot be restarted mid-run.
    assert listed["capture:synth"]["restartable"] is False
    assert "degradation" in listed


def test_only_an_administrator_may_restart_a_worker(rig: Rig) -> None:
    set_up(rig)
    make_user(rig, "oscar", "operator")
    with signed_in(rig, "oscar", GOOD + "?") as o:
        assert o.get("/api/v1/system/workers").status_code == 200
        assert o.post("/api/v1/system/workers/sampler/restart").status_code == 403


def test_a_restart_is_done_and_audited(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        before = rig.app.supervisor
        result = a.post("/api/v1/system/workers/sampler/restart")
        assert result.status_code == 200 and result.json() == {"ok": True, "detail": "restarted"}
        assert workers(a)["sampler"]["alive"] is True
        assert before is rig.app.supervisor
        events = [e.action for e in rig.service.audit.entries(limit=20)]
    assert "worker.restart" in events


def test_a_worker_that_holds_state_is_refused_in_plain_words(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        body = a.post("/api/v1/system/workers/inference/restart").json()
    assert body["ok"] is False and "restart the engine" in body["detail"]


def test_an_unknown_worker_is_not_found(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        assert a.post("/api/v1/system/workers/nonsense/restart").status_code == 404


def test_a_replayed_camera_is_refused_with_the_reason(
    make_config: MakeConfig, tmp_path: Path
) -> None:
    with build_rig(make_config, tmp_path, extra={"cameras": [CAMERA]}) as rig:
        set_up(rig)
        with signed_in(rig, "alice") as a:
            body = a.post("/api/v1/system/workers/capture:synth/restart").json()
    assert body["ok"] is False and "replayed file" in body["detail"]


def test_a_restart_straight_after_another_is_refused(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        assert a.post("/api/v1/system/workers/sampler/restart").json()["ok"] is True
        again = a.post("/api/v1/system/workers/sampler/restart").json()
    assert again["ok"] is False and "moments ago" in again["detail"]


def test_health_reports_a_normal_ladder_by_default(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        body = a.get("/api/v1/system/health").json()
    assert body["degradation"]["step"] == 0 and body["degradation"]["suspended_cameras"] == []


def test_a_degraded_engine_says_so_in_health_and_names_the_reason(
    make_config: MakeConfig, tmp_path: Path
) -> None:
    with build_rig(make_config, tmp_path, extra={**SLOW_LADDER, "cameras": [CAMERA]}) as rig:
        set_up(rig)
        controller = rig.app.degradation
        assert controller is not None
        rig.app.metrics.counter("vigil_frames_analysis_dropped_total", camera="synth").inc(50)
        controller.tick()
        with signed_in(rig, "alice") as a:
            body = a.get("/api/v1/system/health").json()
    assert body["degradation"]["step"] == 1
    assert "dropped 50 frames" in body["degradation"]["reason"]
    assert body["level"] == "degraded"
    assert any("degraded to step 1" in issue for issue in body["issues"])
