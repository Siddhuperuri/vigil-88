"""Failure drills at the engine level: disk full, and a camera that is unplugged and comes back.

These run the real application (real threads, real database, real evidence writer, real scene and
event chain) and break one thing on purpose. They prove the engine's behaviour; they do **not**
prove the behaviour of a real full volume or a real unplugged USB camera, which need hardware and
are drilled by hand (docs/verification/03-failure-drills.md).

What each drill asserts is what an operator is owed:

  * the engine stays up and keeps saying what it is doing;
  * a real incident is still recorded, because losing the record of a real incident is the worst
    available outcome (01 §6);
  * whatever could not be saved is *named* as missing, never replaced by a placeholder;
  * once the cause is gone, the next thing works again without anyone restarting anything.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections import namedtuple
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from tests.integration.test_incident_workflow import (
    OVERRIDES,
    PacedSource,
    ScriptedDetector,
    _all,
    _camera,
    _incident_count,
    _write_zone_file,
    wait_for,
)
from vigil.config.loader import LoadedConfig
from vigil.core.errors import SourceError
from vigil.core.protocols.source import RawFrame, SourceInfo
from vigil.domain.enums import CameraState, EvidenceKind
from vigil.pipeline.app import Application
from vigil.response.evidence import EvidenceStatus

MakeConfig = Callable[..., LoadedConfig]
Usage = namedtuple("Usage", "total used free")


# ------------------------------------------------------------------ disk full


@pytest.fixture
def full_disk(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """While the event is set, the evidence volume reports one megabyte free."""
    real = shutil.disk_usage
    nearly_full = threading.Event()
    nearly_full.set()

    def usage(path: object) -> Usage:
        if nearly_full.is_set():
            return Usage(100 * 1024**3, 100 * 1024**3 - 1024**2, 1024**2)
        return real(path)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr("vigil.response.evidence.shutil.disk_usage", usage)
    return nearly_full


def test_a_full_disk_loses_the_evidence_but_never_the_incident(
    make_config: MakeConfig, full_disk: threading.Event
) -> None:
    loaded = make_config({**OVERRIDES, "cameras": [_camera()]})
    _write_zone_file(loaded.paths.config_dir)
    source = PacedSource()
    app = Application(
        loaded.settings,
        loaded.paths,
        detector_factory=lambda _ctx: ScriptedDetector(),
        source_factory=lambda _camera: source,
    )
    app.start()
    try:
        assert wait_for(lambda: _incident_count(app) >= 1)
        response = app.response
        assert response is not None
        incident = response.incidents.page(_all(), limit=1).incidents[0]  # type: ignore[arg-type]

        # The record exists and is complete.
        assert incident.status.value == "confirmed" and incident.explanation.observations
        # Evidence is reported missing, with a reason, and no file pretends otherwise.
        assert wait_for(
            lambda: (
                response.incidents.evidence_status(incident.incident_id)
                in {EvidenceStatus.UNAVAILABLE.value, EvidenceStatus.PARTIAL.value}
            )
        )
        assert response.evidence.for_incident(incident.incident_id) == ()
        manifest = next(iter(loaded.paths.evidence_dir.rglob("manifest.json")), None)
        if manifest is not None:  # when one could be written it names what is missing, and why
            body = json.loads(manifest.read_text(encoding="utf-8"))
            assert body["files"] == [] and body["status"] != "complete"
            assert any("nearly full" in m["reason"] for m in body["missing"])
        # The engine is still running and still analysing; a full disk is not a crash.
        assert app.snapshot().state.value == "running"
        assert all(not w.crashed for w in app.snapshot().workers)
        assert app.metrics.counter_total("vigil_evidence_failures_total") >= 1
    finally:
        source.halt()
        app.stop()


def test_once_the_disk_has_room_again_evidence_is_written_again(
    make_config: MakeConfig, full_disk: threading.Event
) -> None:
    """Recovery needs no restart: the same writer, asked again, succeeds."""
    loaded = make_config({**OVERRIDES, "cameras": [_camera()]})
    _write_zone_file(loaded.paths.config_dir)
    app = Application(
        loaded.settings,
        loaded.paths,
        detector_factory=lambda _ctx: ScriptedDetector(),
        source_factory=lambda _camera: PacedSource(),
    )
    app.start()
    try:
        assert wait_for(lambda: _incident_count(app) >= 1)
        response = app.response
        assert response is not None
        full_disk.clear()  # the volume has room again
        session = response.capture._writer.open(
            camera_id="gate-north",
            incident_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            confirmed_wall_utc=datetime(2026, 3, 1, tzinfo=UTC),
        )
        assert session is not None
        written = session.write(
            EvidenceKind.SNAPSHOT_RAW,
            "snapshot_raw.jpg",
            b"\xff\xd8\xff\xe0data",
            media_type="image/jpeg",
        )
        assert written is not None and written.size_bytes > 0
    finally:
        app.stop()


# ------------------------------------------------------------------ camera unplugged


class UnpluggableSource(PacedSource):
    """Delivers frames, then fails every read as a pulled USB cable would, then recovers."""

    def __init__(self) -> None:
        super().__init__(interval_s=0.03)
        self.unplugged = threading.Event()
        self.plugged_in = threading.Event()
        self.opens = 0

    def open(self) -> SourceInfo:
        if self.unplugged.is_set() and not self.plugged_in.is_set():
            self._open = False
            raise SourceError("the camera is not connected")
        self.opens += 1
        return super().open()

    def read(self) -> RawFrame | None:
        if self.unplugged.is_set() and not self.plugged_in.is_set():
            self._open = False
            raise SourceError("the camera was unplugged")
        return super().read()


def _state(app: Application) -> CameraState:
    return app.camera_health()[0].state


def test_an_unplugged_camera_is_shown_as_not_working_then_comes_back_by_itself(
    make_config: MakeConfig,
) -> None:
    loaded = make_config(
        {
            **OVERRIDES,
            "ingest.reconnect_base_ms": 50,
            "ingest.reconnect_max_ms": 200,
            "ingest.max_consecutive_read_failures": 2,
            "cameras": [_camera()],
        }
    )
    _write_zone_file(loaded.paths.config_dir)
    source = UnpluggableSource()
    app = Application(
        loaded.settings,
        loaded.paths,
        detector_factory=lambda _ctx: ScriptedDetector(),
        source_factory=lambda _camera: source,
    )
    app.start()
    try:
        assert wait_for(lambda: _state(app) is CameraState.ONLINE)
        epoch_before = app.camera_health()[0].stream_epoch
        assert wait_for(lambda: _incident_count(app) >= 1)  # something real to be interrupted

        source.unplugged.set()  # pull the cable
        assert wait_for(lambda: _state(app) in {CameraState.RECONNECTING, CameraState.FAILED})
        # Honest in the meantime: it is not online, and the engine says why.
        health = app.snapshot()
        assert health.cameras[0].state is not CameraState.ONLINE
        assert health.cameras[0].detail
        assert "camera" in " ".join(health.health.issues).lower()
        assert health.state.value == "running"  # a missing camera is not an engine failure

        source.plugged_in.set()  # plug it back in
        assert wait_for(lambda: _state(app) is CameraState.ONLINE, timeout_s=30.0)
        after = app.camera_health()[0]
        assert after.stream_epoch > epoch_before, "the reconnect started a new stream"
        assert source.opens >= 2

        # The interruption is on the record, on the same axis as incidents (08 §3).
        response = app.response
        assert response is not None
        events = [e.state for e in response.health_events.recent(limit=200)]
        assert CameraState.RECONNECTING in events or CameraState.FAILED in events
        assert events.count(CameraState.ONLINE) >= 2

        # And the ladder did not mistake a missing camera for overload.
        assert app.degradation is not None and app.degradation.state.step == 0
    finally:
        source.halt()
        app.stop()


def test_footage_across_the_unplugged_gap_is_marked_not_passed_off_as_continuous(
    make_config: MakeConfig,
) -> None:
    loaded = make_config(
        {
            **OVERRIDES,
            "ingest.reconnect_base_ms": 50,
            "ingest.reconnect_max_ms": 200,
            "ingest.max_consecutive_read_failures": 2,
            "evidence.gap_threshold_ms": 250,
            "cameras": [_camera()],
        }
    )
    _write_zone_file(loaded.paths.config_dir)
    source = UnpluggableSource()
    app = Application(
        loaded.settings,
        loaded.paths,
        detector_factory=lambda _ctx: ScriptedDetector(),
        source_factory=lambda _camera: source,
    )
    app.start()
    try:
        assert wait_for(lambda: _state(app) is CameraState.ONLINE)
        assert app.response is not None
        sampler = app.response.sampler
        assert wait_for(
            lambda: (b := sampler.buffer_for("gate-north")) is not None and b.frame_count > 2
        )
        source.unplugged.set()
        assert wait_for(lambda: _state(app) is not CameraState.ONLINE)
        time.sleep(0.6)  # long enough to be a hole, not a hiccup
        source.plugged_in.set()
        assert wait_for(lambda: _state(app) is CameraState.ONLINE, timeout_s=30.0)
        buffer = sampler.buffer_for("gate-north")
        assert buffer is not None
        assert wait_for(lambda: bool(buffer.gaps_in(since_ns=0, until_ns=10**18)), timeout_s=20.0)
        kinds = {g.kind for g in buffer.gaps_in(since_ns=0, until_ns=10**18)}
        assert kinds & {"stream_restarted", "frame_gap"}
    finally:
        source.halt()
        app.stop()
