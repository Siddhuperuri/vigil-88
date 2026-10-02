"""Protocol modules name domain types only under TYPE_CHECKING, so nothing at runtime would
notice a broken one. These tests import every protocol module and exercise its dataclasses."""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import vigil.core.protocols as protocols
from tests.support.fakes import StubPixels
from vigil.core.capabilities import Capability
from vigil.core.protocols.module import ModuleContext, ModuleDescriptor
from vigil.core.protocols.severity import SeverityContext
from vigil.core.protocols.source import RawFrame, SourceInfo, SourceTiming
from vigil.core.protocols.validator import VerificationContext
from vigil.domain import EventType
from vigil.observability.logging import get_logger

MODULE_NAMES = sorted(m.name for m in pkgutil.iter_modules(protocols.__path__))


def test_every_engine_has_a_protocol_module() -> None:
    assert set(MODULE_NAMES) >= {
        "channel",
        "detector",
        "logger",
        "module",
        "pixels",
        "scene",
        "severity",
        "source",
        "temporal",
        "tracker",
        "transport",
        "validator",
    }


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_protocol_module_imports_at_runtime(name: str) -> None:
    importlib.import_module(f"vigil.core.protocols.{name}")


def test_module_descriptor_and_context() -> None:
    d = ModuleDescriptor(
        module_id="intrusion",
        event_type=EventType.INTRUSION,
        version="1.0.0",
        requires=frozenset({Capability.DETECTION, Capability.ZONES}),
        optional=frozenset({Capability.VELOCITY}),
        default_params={"base_signal": 0.7},
        description="person in a restricted zone",
    )
    assert d.requires == {Capability.DETECTION, Capability.ZONES}
    ctx = ModuleContext("cam-a", {"base_signal": 0.5}, get_logger("t"))
    assert ctx.params["base_signal"] == 0.5


def test_source_dataclasses_and_timing_semantics() -> None:
    info = SourceInfo(640, 480, None, "scripted", None)
    assert info.fps is None  # unknown is None, not 0
    raw = RawFrame(StubPixels())
    assert raw.source_pts_ms is None and raw.is_keyframe is None
    assert {t.value for t in SourceTiming} == {"live", "paced", "one_shot"}


def test_verification_and_severity_contexts() -> None:
    v = VerificationContext(now_monotonic_ns=5, camera_health=None, scene=None)
    assert v.now_monotonic_ns == 5
    from datetime import UTC, datetime

    s = SeverityContext(
        datetime(2026, 1, 1, tzinfo=UTC), camera_trust=1.0, related_incident_count=0
    )
    assert s.camera_trust == 1.0
