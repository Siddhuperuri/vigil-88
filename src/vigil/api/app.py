"""FastAPI assembly (08 §11). Reads immutable snapshots; never touches a pipeline lock beyond
the results store's short critical section, so a slow or hung browser cannot stall detection."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from vigil.api.schemas import (
    CameraOut,
    CapabilityOut,
    DetectionsOut,
    HealthOut,
    LatencyOut,
    MetricsOut,
    ModelOut,
    SystemOut,
)
from vigil.api.security import make_auth_dependency
from vigil.api.viewer import VIEWER_HTML
from vigil.core.protocols.transport import StreamKey
from vigil.core.units import NS_PER_S, ns_to_ms
from vigil.observability.metrics import HistogramSummary
from vigil.pipeline.annotate import OverlayOptions, RenderError, parse_overlay, render_jpeg
from vigil.pipeline.app import Application
from vigil.pipeline.streaming import StreamHub
from vigil.version import __version__

BOUNDARY = "frame"
MIN_STREAM_FPS = 0.5
MAX_STREAM_FPS = 60.0
WAIT_SLICE_S = 0.5  # also how quickly a departed viewer is noticed
_CAMERA_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,38}$")
_COUNTERS = (
    "vigil_frames_received_total",
    "vigil_frames_skipped_total",
    "vigil_frames_dropped_total",
    "vigil_frames_inferred_total",
    "vigil_detections_total",
    "vigil_inference_errors_total",
    "vigil_detector_fallbacks_total",
    "vigil_decode_errors_total",
    "vigil_stream_errors_total",
    "vigil_worker_crashes_total",
)


def _overlay(text: str | None) -> OverlayOptions:
    try:
        return parse_overlay(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def create_app(application: Application, hub: StreamHub) -> FastAPI:
    cfg = application.settings.api
    auth = make_auth_dependency(cfg.auth.enabled, cfg.auth.token)
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)])
    metrics = application.metrics

    def known_camera(camera_id: str) -> str:
        if not _CAMERA_ID.match(camera_id) or camera_id not in {
            c.camera_id for c in application.camera_health()
        }:
            raise HTTPException(status_code=404, detail=f"unknown camera {camera_id!r}")
        return camera_id

    # ---------------------------------------------------------------- system

    @api.get("/system/health", response_model=HealthOut)
    def health() -> HealthOut:
        snap = application.snapshot()
        return HealthOut(
            state=snap.state.value,
            level=snap.health.level.value,
            issues=list(snap.health.issues),
            uptime_ms=snap.uptime_ms,
            detector=ModelOut.of(snap.detector) if snap.detector else None,
            capabilities=[
                CapabilityOut(
                    capability=c.capability.value,
                    available=c.available,
                    provider=c.provider,
                    how_to_provide=c.how_to_provide,
                )
                for c in snap.capabilities
            ],
        )

    @api.get("/system/info")
    def info() -> dict[str, object]:
        snap = application.snapshot()
        return {
            "version": __version__,
            "config_hash": application.settings.config_hash(),
            "detector": ModelOut.of(snap.detector).model_dump() if snap.detector else None,
        }

    @api.get("/system/metrics", response_model=MetricsOut)
    def system_metrics() -> MetricsOut:
        snap = application.snapshot()
        raw = metrics.snapshot()

        def by_label(name: str, label: str) -> dict[str, LatencyOut]:
            out: dict[str, LatencyOut] = {}
            for (n, labels), summary in raw.histograms.items():
                if n == name and dict(labels).get(label):
                    out[dict(labels)[label]] = LatencyOut.of(summary)
            return out

        empty = HistogramSummary(0, 0, None, None, None, None, None, None)
        inference = raw.histograms.get(("vigil_inference_latency_ms", ()), empty)
        return MetricsOut(
            system=SystemOut.of(snap.system) if snap.system else None,
            pipeline_fps=metrics.gauge("vigil_pipeline_fps").value,
            inference_latency=LatencyOut.of(inference),
            stage_latency=by_label("vigil_stage_latency_ms", "stage"),
            capture_to_result=by_label("vigil_capture_to_infer_ms", "camera"),
            end_to_end=by_label("vigil_end_to_end_ms", "camera"),
            counters={n.removeprefix("vigil_"): metrics.counter_total(n) for n in _COUNTERS},
            cameras=[CameraOut.of(c) for c in snap.cameras],
        )

    # ---------------------------------------------------------------- cameras

    @api.get("/cameras", response_model=list[CameraOut])
    def cameras() -> list[CameraOut]:
        return [CameraOut.of(c) for c in application.camera_health()]

    @api.get("/cameras/{camera_id}", response_model=CameraOut)
    def camera(camera_id: str) -> CameraOut:
        known_camera(camera_id)
        return next(
            CameraOut.of(c) for c in application.camera_health() if c.camera_id == camera_id
        )

    @api.get("/cameras/{camera_id}/detections", response_model=DetectionsOut)
    def detections(camera_id: str) -> DetectionsOut:
        known_camera(camera_id)
        analysed = application.results.latest(camera_id)
        if analysed is None:
            raise HTTPException(status_code=404, detail="no frame has been analysed yet")
        return DetectionsOut.of(analysed.result)

    @api.get("/cameras/{camera_id}/snapshot.jpg")
    def snapshot(
        camera_id: str,
        overlay: Annotated[str | None, Query(max_length=40)] = None,
    ) -> Response:
        known_camera(camera_id)
        analysed = application.results.latest(camera_id)
        if analysed is None:
            raise HTTPException(status_code=404, detail="no frame has been analysed yet")
        age_ms = ns_to_ms(application.clock.monotonic_ns() - analysed.completed_monotonic_ns)
        try:
            jpeg = render_jpeg(
                analysed, _overlay(overlay), jpeg_quality=cfg.stream_jpeg_quality, age_ms=age_ms
            )
        except RenderError as exc:
            raise HTTPException(status_code=500, detail=exc.message) from None
        meta = analysed.frame.meta
        return Response(
            jpeg,
            media_type="image/jpeg",
            headers={
                "X-Frame-Index": str(meta.frame_index),
                "X-Stream-Epoch": str(meta.stream_epoch),
                "Cache-Control": "no-store",
            },
        )

    @api.get("/cameras/{camera_id}/stream.mjpeg")
    async def stream(
        request: Request,
        camera_id: str,
        overlay: Annotated[str | None, Query(max_length=40)] = None,
        fps: Annotated[float | None, Query(ge=MIN_STREAM_FPS, le=MAX_STREAM_FPS)] = None,
    ) -> StreamingResponse:
        known_camera(camera_id)
        key = StreamKey(camera_id, _overlay(overlay).key)
        min_interval_ns = round(NS_PER_S / fps) if fps else 0

        async def frames() -> AsyncIterator[bytes]:
            last_seq, last_sent = 0, -min_interval_ns
            with hub.subscribe(key):  # released when the client goes away
                # Disconnects are polled explicitly: a camera that stops producing frames never
                # yields, so the server would otherwise never notice the viewer had left and the
                # viewer slot (and its encoder) would leak until shutdown.
                while not await request.is_disconnected():
                    published = await run_in_threadpool(hub.wait_next, key, last_seq, WAIT_SLICE_S)
                    if published is None:
                        if hub.closed:
                            return
                        continue
                    last_seq = published.seq
                    now = application.clock.monotonic_ns()
                    if now - last_sent < min_interval_ns:
                        continue  # this viewer asked for a lower rate than the stream runs at
                    last_sent = now
                    yield (
                        (
                            f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(published.jpeg)}\r\n\r\n"
                        ).encode()
                        + published.jpeg
                        + b"\r\n"
                    )

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store"},
        )

    app = FastAPI(title="VIGIL-88", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.include_router(api)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def viewer() -> str:
        return VIEWER_HTML

    return app
