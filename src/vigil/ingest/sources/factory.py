"""Source construction. Kinds that are not implemented fail loudly with an explanation;
nothing here pretends (README rule 1)."""

from __future__ import annotations

from vigil.core.clock import Clock
from vigil.core.errors import CapabilityError
from vigil.core.protocols.source import FrameSource
from vigil.domain.camera import SourceSpec
from vigil.domain.enums import SourceKind

IMPLEMENTED_KINDS = frozenset(
    {SourceKind.WEBCAM, SourceKind.SYNTHETIC, SourceKind.VIDEO_FILE, SourceKind.RTSP}
)

_NOT_IMPLEMENTED = {
    SourceKind.IMAGE: "image input is not implemented in this build",
}
_EXTRA_HINT = "install the capture extra: uv sync --extra capture"


def build_source(spec: SourceSpec, clock: Clock | None = None) -> FrameSource:
    if spec.kind in _NOT_IMPLEMENTED:
        raise CapabilityError(_NOT_IMPLEMENTED[spec.kind], context={"kind": spec.kind.value})
    try:
        if spec.kind is SourceKind.WEBCAM:
            from vigil.ingest.sources.webcam import WebcamSource

            return WebcamSource(spec)
        if spec.kind is SourceKind.VIDEO_FILE:
            from vigil.ingest.sources.video_file import VideoFileSource

            return VideoFileSource(spec)
        if spec.kind is SourceKind.RTSP:
            from vigil.ingest.sources.rtsp import RtspSource

            return RtspSource(spec)
        if spec.kind is SourceKind.SYNTHETIC:
            from vigil.ingest.sources.synthetic import SyntheticSource

            return SyntheticSource(spec, clock)
    except ImportError as exc:
        raise CapabilityError(
            f"{spec.kind.value} source needs OpenCV/NumPy, which are not installed: {_EXTRA_HINT}"
        ) from exc
    raise CapabilityError(f"unknown source kind {spec.kind.value!r}")  # pragma: no cover
