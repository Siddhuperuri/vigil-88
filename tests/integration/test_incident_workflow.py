"""Frames to a persisted, evidenced incident, through the real application (P4 exit criteria).

The P3 scenario tests drive the chain from `SceneState` and prove the *decisions* are right.
This one starts from frames and proves the **wiring** is right: capture, inference, tracking,
scene assembly, the event chain, incident persistence, evidence capture and the lifecycle all
hold hands in one running `Application`, on real threads, against a real SQLite file.

So the thresholds here are deliberately shortened (`min_duration_ms`, `postroll_seconds`): this
test is not about how long the system waits before confirming -- that is tuned and asserted in
`test_intrusion_scenarios.py` -- and a test that spent fifteen seconds of wall clock proving a
hand-off would not get run.

What is *not* shortened is anything structural: the real detector protocol, the real tracker,
the real zone file on disk, the real schema, the real evidence writer.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest

from vigil.config.loader import LoadedConfig
from vigil.core.capabilities import Capability
from vigil.core.geometry import BBox
from vigil.core.protocols.source import RawFrame, SourceInfo, SourceTiming
from vigil.domain.camera import SourceSpec
from vigil.domain.detection import Detection, DetectionSet, ModelDescriptor, PreparedFrame
from vigil.domain.enums import EventType, EvidenceKind, IncidentStatus, SourceKind
from vigil.ingest.pixel_buffer import NumpyPixelBuffer
from vigil.pipeline.app import Application
from vigil.response.evidence import EvidenceStatus, sha256_file
from vigil.response.paths import resolve_relative

CAMERA = "gate-north"
ZONES_REF = "yard"
WIDTH, HEIGHT = 320, 240
# The zone covers the lower-right quadrant; the detection's foot point sits inside it.
ZONE_YAML = """
version: 1
zones:
  - zone_id: yard
    kind: restricted
    criticality: 3
    label: North yard
    polygon: [[0.45, 0.35], [0.98, 0.35], [0.98, 0.98], [0.45, 0.98]]
"""
PERSON_BOX = BBox(170.0, 90.0, 230.0, 210.0)  # foot point (200, 210) -> inside the zone
MODEL = ModelDescriptor(
    name="scripted",
    version="1.0",
    backend="scripted",
    weights_sha256=None,
    input_size_px=None,
    precision="fp32",
    device="cpu",
    license=None,
)
WAIT_S = 20.0


class PacedSource:
    """A live source that produces real frames at a fixed interval until stopped."""

    def __init__(self, *, interval_s: float = 0.04) -> None:
        self._interval_s = interval_s
        self._open = False
        self._stop = threading.Event()
        self.read_calls = 0

    @property
    def spec(self) -> SourceSpec:
        return SourceSpec(SourceKind.SYNTHETIC, "paced", {})

    @property
    def timing(self) -> SourceTiming:
        return SourceTiming.LIVE

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> SourceInfo:
        self._open = True
        return SourceInfo(WIDTH, HEIGHT, 25.0, "paced", "test pattern")

    def read(self) -> RawFrame | None:
        if self._stop.wait(self._interval_s):
            return None
        self.read_calls += 1
        # A mid-grey frame with a lighter block, so the stability probe has real structure to
        # compare and the JPEG encoder has something to encode.
        pixels = np.full((HEIGHT, WIDTH, 3), 90, dtype=np.uint8)
        pixels[90:210, 170:230] = 160
        return RawFrame(NumpyPixelBuffer(pixels), source_pts_ms=None)

    def close(self) -> None:
        self._open = False

    def halt(self) -> None:
        self._stop.set()


class ScriptedDetector:
    """Reports the same person in the same place on every frame.

    A per-frame classifier would call that hundreds of incidents; the platform must call it one.
    """

    def __init__(self) -> None:
        self.calls = 0

    @property
    def descriptor(self) -> ModelDescriptor:
        return MODEL

    @property
    def provides(self) -> frozenset[Capability]:
        return frozenset({Capability.DETECTION})

    def warmup(self) -> None:
        return None

    def detect_batch(self, frames: Sequence[PreparedFrame]) -> Sequence[DetectionSet]:
        self.calls += 1
        return [
            DetectionSet(
                frame_meta=prepared.frame.meta,
                detections=(Detection(PERSON_BOX, _person(), 0.92, "person"),),
                model=MODEL,
                inference_latency_ms=1.5,
            )
            for prepared in frames
        ]

    def close(self) -> None:
        return None


def _person() -> object:
    from vigil.domain.enums import ObjectClass

    return ObjectClass.PERSON


OVERRIDES: dict[str, object] = {
    "modules.enabled": ["intrusion"],
    # Shortened: this test is about the wiring, not the tuning (see the module docstring).
    "temporal.min_duration_ms": 300,
    "temporal.min_observations": 3,
    "temporal.release_sustain_ms": 200,
    "verification.min_track_age_ms": 100.0,
    "evidence.postroll_seconds": 0.4,
    "evidence.preroll_seconds": 1.0,
    "evidence.preroll_fps": 10.0,
    "retention.sweep_interval_s": 3600,
    "pipeline.global_inference_fps": 25.0,
    "alerts.channels": ["console", "jsonl"],
    "alerts.min_severity": "INFO",
}


def _camera() -> dict[str, object]:
    return {
        "camera_id": CAMERA,
        "name": "North gate",
        "location": "North gate, pole 3",
        "source": {"kind": "synthetic"},
        "enabled": True,
        "zones_ref": ZONES_REF,
        "target_inference_fps": 20.0,
    }


MakeConfig = Callable[..., LoadedConfig]


def _write_zone_file(config_dir: Path) -> None:
    zones = config_dir / "zones"
    zones.mkdir(parents=True, exist_ok=True)
    (zones / f"{ZONES_REF}.zones.yaml").write_text(ZONE_YAML, encoding="utf-8")


def wait_for(predicate: Callable[[], bool], *, timeout_s: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def running(make_config: MakeConfig) -> Iterator[tuple[Application, LoadedConfig, PacedSource]]:
    loaded = make_config({**OVERRIDES, "cameras": [_camera()]})
    _write_zone_file(loaded.paths.config_dir)
    source = PacedSource()
    detector = ScriptedDetector()
    app = Application(
        loaded.settings,
        loaded.paths,
        detector_factory=lambda _ctx: detector,
        source_factory=lambda _camera: source,
    )
    app.start()
    try:
        yield (app, loaded, source)
    finally:
        source.halt()
        app.stop()


def _incident_count(app: Application) -> int:
    response = app.response
    assert response is not None
    return response.incidents.count(_all())


def _all() -> object:
    from vigil.persistence.queries import IncidentFilter

    return IncidentFilter()


# ------------------------------------------------------------------ the headline


def test_frames_become_exactly_one_persisted_incident(running: object) -> None:
    app, _loaded, _source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)

    response = app.response
    assert response is not None
    page = response.incidents.page(_all(), limit=10)  # type: ignore[arg-type]
    assert page.total == 1, "a sustained intrusion is one incident, not one per frame"

    incident = page.incidents[0]
    assert incident.event_type is EventType.INTRUSION
    assert incident.camera_id == CAMERA
    assert incident.module_id == "intrusion"
    assert incident.status is IncidentStatus.CONFIRMED
    assert incident.display_ref.startswith("INC-")
    # Denormalized at creation, so the incident stays readable if the camera is renamed (05 §4).
    assert incident.location == "North gate, pole 3"
    assert incident.detection_latency_ms > 0.0
    assert incident.confidence_ratio > 0.5


def test_the_incident_explains_itself(running: object) -> None:
    app, _loaded, _source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)
    response = app.response
    assert response is not None
    incident = response.incidents.page(_all(), limit=1).incidents[0]  # type: ignore[arg-type]

    explanation = incident.explanation
    assert explanation.trigger_summary
    assert explanation.observations, "the observations that caused promotion are recorded"
    assert explanation.accumulator_trace, "and how confidence got there"
    # Every validator that ran, including the ones that passed (06 §6).
    assert len(explanation.validator_verdicts) >= 8
    assert {f.name for f in explanation.severity_factors} >= {"base", "confidence"}
    # The thresholds in force at the time, not today's.
    assert explanation.thresholds["temporal.min_duration_ms"] == 300.0

    contribution = sum(f.contribution for f in explanation.severity_factors)
    assert contribution * 100 == pytest.approx(incident.severity_score, abs=0.5)


def test_the_lifecycle_is_an_audit_trail(running: object) -> None:
    app, _loaded, _source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)
    response = app.response
    assert response is not None
    incident = response.incidents.page(_all(), limit=1).incidents[0]  # type: ignore[arg-type]
    transitions = response.incidents.transitions(incident.incident_id)

    assert [t.to_status for t in transitions][:2] == [
        IncidentStatus.CANDIDATE,
        IncidentStatus.CONFIRMED,
    ]
    assert all(t.actor == "system" for t in transitions[:2])
    assert all(t.reason for t in transitions), "every transition states why"


def test_a_sustained_situation_extends_rather_than_duplicating(running: object) -> None:
    app, _loaded, _source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)
    response = app.response
    assert response is not None
    first = response.incidents.page(_all(), limit=1).incidents[0]  # type: ignore[arg-type]

    # Let the module keep reporting for a while: the span must grow, the count must not.
    # A wall-clock budget, not an assertion: the assertion is that the span grows and the count
    # does not. This drives a real Application on real threads and waits for the extension
    # cadence, so under `--cov` on a loaded machine 10 s was occasionally not enough and the
    # test failed intermittently in the full gate. Widened to 30 s, which still sits inside
    # pytest's own 60 s per-test timeout.
    assert wait_for(
        lambda: (
            response.incidents.get(first.incident_id) is not None
            and _last_observed(response, first.incident_id) > first.last_observed_wall_utc
        ),
        timeout_s=30.0,
    )
    assert response.incidents.count(_all()) == 1  # type: ignore[arg-type]
    assert response.worker.stats.incidents_extended > 0


def _last_observed(response: object, incident_id: str) -> object:
    lifecycle = response.incidents.lifecycle(incident_id)  # type: ignore[attr-defined]
    assert lifecycle is not None
    return lifecycle.last_observed_wall_utc


# ------------------------------------------------------------------ evidence


def test_evidence_is_written_hashed_and_self_describing(running: object) -> None:
    app, loaded, _source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)
    response = app.response
    assert response is not None
    incident = response.incidents.page(_all(), limit=1).incidents[0]  # type: ignore[arg-type]

    assert wait_for(lambda: bool(response.evidence.for_incident(incident.incident_id)))
    records = response.evidence.for_incident(incident.incident_id)
    kinds = {r.kind for r in records}
    assert EvidenceKind.EXPLANATION in kinds
    assert EvidenceKind.SCENE_DUMP in kinds
    assert EvidenceKind.TRACK_DUMP in kinds
    assert EvidenceKind.SNAPSHOT_RAW in kinds

    for record in records:
        path = resolve_relative(loaded.paths.evidence_dir, record.relative_path)
        assert path.is_file(), record.relative_path
        assert path.stat().st_size == record.size_bytes
        assert sha256_file(path) == record.sha256, "the recorded hash is the file's hash"
        # Relative and inside the evidence directory (01 §10).
        assert not Path(record.relative_path).is_absolute()
        assert ".." not in record.relative_path

    manifest = resolve_relative(
        loaded.paths.evidence_dir, f"{records[0].relative_path.rsplit('/', 1)[0]}/manifest.json"
    )
    assert manifest.is_file(), "an evidence directory is self-describing (05 §6)"


def test_the_evidence_status_is_reported_honestly(running: object) -> None:
    app, _loaded, _source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)
    response = app.response
    assert response is not None
    incident = response.incidents.page(_all(), limit=1).incidents[0]  # type: ignore[arg-type]

    assert wait_for(
        lambda: (
            response.incidents.evidence_status(incident.incident_id)
            in {EvidenceStatus.COMPLETE.value, EvidenceStatus.PARTIAL.value}
        )
    )
    status = response.incidents.evidence_status(incident.incident_id)
    # Either is honest. COMPLETE means every requested item landed; PARTIAL means some did not
    # and the manifest says which -- on a build with no MP4 encoder, PARTIAL is the truth.
    assert status in {EvidenceStatus.COMPLETE.value, EvidenceStatus.PARTIAL.value}


def test_evidence_memory_stays_inside_the_configured_ceiling(running: object) -> None:
    """The P4 exit criterion: measured, not assumed."""
    app, loaded, _source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)
    response = app.response
    assert response is not None
    ceiling = loaded.settings.evidence.preroll_max_mb * 1024 * 1024
    for _ in range(40):
        assert response.sampler.total_bytes() <= ceiling
        time.sleep(0.02)


# ------------------------------------------------------------------ candidates and alerts


def test_every_candidate_outcome_is_persisted(running: object) -> None:
    """Rejections are data: without them you are tuning suppression blind (06 §6)."""
    app, _loaded, _source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)
    response = app.response
    assert response is not None
    assert wait_for(lambda: bool(response.candidates.recent(limit=5)))
    rows = response.candidates.recent(limit=5)
    assert rows[0].camera_id == CAMERA
    assert rows[0].module_id == "intrusion"
    assert rows[0].observations >= 3
    assert rows[0].incident_id is not None, "a verified candidate names the incident it became"


def test_an_alert_reaches_the_configured_channels(running: object) -> None:
    app, loaded, _source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)
    response = app.response
    assert response is not None
    assert wait_for(lambda: response.worker.stats.alerts_dispatched >= 1)
    assert wait_for(lambda: loaded.paths.alerts_file.is_file())
    body = loaded.paths.alerts_file.read_text(encoding="utf-8")
    assert "incident_id" in body
    channels = {s.channel_id: s.available for s in response.alerts.status()}
    assert channels == {"console": True, "jsonl": True}


# ------------------------------------------------------------------ restart


def test_an_incident_and_its_evidence_survive_a_restart(make_config: MakeConfig) -> None:
    """P4 exit criterion: an incident survives a restart with evidence intact and hashes
    verified. Two separate `Application` instances over one data directory."""
    loaded = make_config({**OVERRIDES, "cameras": [_camera()]})
    _write_zone_file(loaded.paths.config_dir)

    source = PacedSource()
    first = Application(
        loaded.settings,
        loaded.paths,
        detector_factory=lambda _ctx: ScriptedDetector(),
        source_factory=lambda _camera: source,
    )
    first.start()
    try:
        assert wait_for(lambda: _incident_count(first) >= 1)
        response = first.response
        assert response is not None
        incident_id = response.incidents.page(_all(), limit=1).incidents[0].incident_id  # type: ignore[arg-type]
        assert wait_for(lambda: bool(response.evidence.for_incident(incident_id)))
    finally:
        source.halt()
        first.stop()

    # A fresh process over the same database and evidence directory.
    second_source = PacedSource()
    second = Application(
        loaded.settings,
        loaded.paths,
        detector_factory=lambda _ctx: ScriptedDetector(),
        source_factory=lambda _camera: second_source,
    )
    second.start()
    try:
        reopened = second.response
        assert reopened is not None
        incident = reopened.incidents.get(incident_id)
        assert incident is not None, "the incident is still there"
        assert incident.explanation.trigger_summary
        assert reopened.incidents.transitions(incident_id)

        records = reopened.evidence.for_incident(incident_id)
        assert records, "and so is its evidence"
        for record in records:
            path = resolve_relative(loaded.paths.evidence_dir, record.relative_path)
            assert sha256_file(path) == record.sha256
    finally:
        second_source.halt()
        second.stop()


def test_the_schema_is_migrated_once_and_recorded(make_config: MakeConfig) -> None:
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
        response = app.response
        assert response is not None
        assert response.migration.version == response.database.known_schema_version()
        assert response.migration.applied  # first open applies it
        version = response.database.schema_version()
    finally:
        source.halt()
        app.stop()

    with sqlite3.connect(loaded.paths.database_file) as connection:
        rows = connection.execute("SELECT version, name FROM schema_version").fetchall()
    assert [r[0] for r in rows] == [version]


def test_shutdown_leaves_the_database_consistent(running: object) -> None:
    """Nothing confirmed is abandoned, and the file is readable afterwards."""
    app, loaded, source = running  # type: ignore[misc]
    assert wait_for(lambda: _incident_count(app) >= 1)
    source.halt()
    report = app.stop()
    assert report.clean, f"stragglers: {report.stragglers}"

    with sqlite3.connect(loaded.paths.database_file) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        incidents = connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        orphans = connection.execute(
            "SELECT COUNT(*) FROM evidence e "
            "WHERE NOT EXISTS (SELECT 1 FROM incidents i WHERE i.incident_id = e.incident_id)"
        ).fetchone()[0]
    assert incidents >= 1
    assert orphans == 0, "evidence never outlives the incident it belongs to"
