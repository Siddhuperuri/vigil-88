"""A hole in footage is recorded, and survives the buffer being cleared (07, reconnects)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from vigil.core.clock import ManualClock
from vigil.core.units import NS_PER_MS
from vigil.domain.frame import FrameMeta
from vigil.ingest.buffers import EncodedFrame, PreRollBuffer, StreamGap
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline import preroll
from vigil.pipeline.preroll import PreRollSampler

CAMERA = "gate-north"


@dataclass
class Latest:
    """Stands in for `LatestDetections`: the sampler reads a version and the latest frame."""

    frame: Any = None
    version_no: int = 0

    def version(self, _camera: str) -> int:
        return self.version_no

    def latest(self, _camera: str) -> Any:
        return self.frame

    def publish(self, *, at_ms: int, epoch: int = 0) -> None:
        meta = FrameMeta(
            camera_id=CAMERA,
            stream_epoch=epoch,
            frame_index=self.version_no,
            t_monotonic_ns=at_ms * NS_PER_MS,
            wall_utc=datetime(2026, 3, 1, tzinfo=UTC),
            width_px=64,
            height_px=48,
            source_pts_ms=None,
            is_keyframe=None,
        )
        self.frame = SimpleNamespace(frame=SimpleNamespace(meta=meta))
        self.version_no += 1


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> tuple[PreRollSampler, Latest, PreRollBuffer]:
    monkeypatch.setattr(preroll, "render_jpeg", lambda *_a, **_k: b"jpeg")
    buffer = PreRollBuffer(window_ms=60_000, max_bytes=10_000_000)
    latest = Latest()
    sampler = PreRollSampler(
        buffers={CAMERA: buffer},
        results=latest,  # type: ignore[arg-type]
        clock=ManualClock(),
        metrics=MetricsRegistry(),
        logger=get_logger("test.preroll"),
        fps=5,
        jpeg_quality=80,
        gap_threshold_ms=2000,
    )
    return sampler, latest, buffer


def feed(rig: tuple[PreRollSampler, Latest, PreRollBuffer], at_ms: int, epoch: int = 0) -> None:
    sampler, latest, _ = rig
    latest.publish(at_ms=at_ms, epoch=epoch)
    sampler.step()


def everything(buffer: PreRollBuffer) -> tuple[StreamGap, ...]:
    return buffer.gaps_in(since_ns=0, until_ns=10**15)


def test_steady_frames_leave_no_gap(rig: tuple[PreRollSampler, Latest, PreRollBuffer]) -> None:
    for at in (1000, 1200, 1400, 1600):
        feed(rig, at)
    assert everything(rig[2]) == ()


def test_a_silence_longer_than_the_threshold_is_recorded_as_a_frame_gap(
    rig: tuple[PreRollSampler, Latest, PreRollBuffer],
) -> None:
    feed(rig, 1000)
    feed(rig, 1200)
    feed(rig, 6200)  # five seconds with nothing, on a stream that never restarted
    (gap,) = everything(rig[2])
    assert gap == StreamGap(1200 * NS_PER_MS, 6200 * NS_PER_MS, "frame_gap")


def test_a_gap_just_under_the_threshold_is_not_a_gap(
    rig: tuple[PreRollSampler, Latest, PreRollBuffer],
) -> None:
    feed(rig, 1000)
    feed(rig, 2900)
    assert everything(rig[2]) == ()


def test_a_restart_records_the_hole_and_still_clears_the_old_view(
    rig: tuple[PreRollSampler, Latest, PreRollBuffer],
) -> None:
    feed(rig, 1000)
    feed(rig, 1200)
    feed(rig, 9000, epoch=1)
    buffer = rig[2]
    assert buffer.frame_count == 1  # only the frame from the new stream
    (gap,) = everything(buffer)
    assert gap.kind == "stream_restarted"
    assert (gap.start_ns, gap.end_ns) == (1200 * NS_PER_MS, 9000 * NS_PER_MS)


def test_a_restart_is_one_gap_not_two(rig: tuple[PreRollSampler, Latest, PreRollBuffer]) -> None:
    feed(rig, 1000)
    feed(rig, 9000, epoch=1)
    assert len(everything(rig[2])) == 1


def test_gaps_in_only_returns_holes_that_overlap_the_window() -> None:
    buffer = PreRollBuffer(window_ms=1000, max_bytes=1000)
    buffer.note_gap(StreamGap(10, 20, "frame_gap"))
    buffer.note_gap(StreamGap(100, 200, "stream_restarted"))
    assert [g.start_ns for g in buffer.gaps_in(since_ns=15, until_ns=50)] == [10]
    assert [g.start_ns for g in buffer.gaps_in(since_ns=150, until_ns=999)] == [100]
    assert buffer.gaps_in(since_ns=300, until_ns=400) == ()


def test_the_gap_list_is_bounded() -> None:
    buffer = PreRollBuffer(window_ms=1000, max_bytes=1000)
    for i in range(100):
        buffer.note_gap(StreamGap(i, i + 1, "frame_gap"))
    assert len(buffer.gaps_in(since_ns=0, until_ns=10**9)) == 32


def test_clear_keeps_the_gaps() -> None:
    buffer = PreRollBuffer(window_ms=1000, max_bytes=10_000)
    buffer.append(EncodedFrame(meta=_meta(5), jpeg=b"x"))
    buffer.note_gap(StreamGap(1, 2, "frame_gap"))
    buffer.clear()
    assert buffer.frame_count == 0 and len(buffer.gaps_in(since_ns=0, until_ns=10)) == 1


def _meta(at_ms: int) -> FrameMeta:
    return FrameMeta(
        camera_id=CAMERA,
        stream_epoch=0,
        frame_index=0,
        t_monotonic_ns=at_ms * NS_PER_MS,
        wall_utc=datetime(2026, 3, 1, tzinfo=UTC),
        width_px=64,
        height_px=48,
        source_pts_ms=None,
        is_keyframe=None,
    )
