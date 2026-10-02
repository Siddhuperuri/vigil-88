"""The HTTP API against a REAL running application: real capture, real ONNX Runtime inference
(on a generated model with a known planted detection), real stream worker."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import cv2
import httpx
import numpy as np
import pytest

from tests.support.onnx_models import PlantedBox, write_model
from vigil.api.app import create_app
from vigil.api.server import ApiServer
from vigil.config.loader import LoadedConfig
from vigil.core.protocols.transport import StreamKey
from vigil.observability.logging import get_logger
from vigil.pipeline.annotate import parse_overlay
from vigil.pipeline.app import Application
from vigil.pipeline.streaming import StreamHub

MakeConfig = Callable[..., LoadedConfig]
# model-space box -> frame 320x240 -> letterbox scale 416/320 = 1.3
PLANTED = PlantedBox(cx=200, cy=150, w=100, h=80, class_id=0, score=0.9)
EXPECTED = (150 / 1.3, 110 / 1.3, 250 / 1.3, 190 / 1.3)  # x1, y1, x2, y2 in source pixels
TOKEN = "t" * 40


def wait_for(cond: Callable[[], bool], timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def build(make_config: MakeConfig, **overrides: object) -> tuple[Application, StreamHub]:
    cfg = make_config(
        {
            "vision.backend": "onnxruntime",
            "vision.weights": "fake_yolox",
            "vision.device": "cpu",
            "cameras": [{"camera_id": "synth", "source": {"kind": "synthetic"}}],
            **overrides,
        }
    )
    write_model(cfg.paths.models_dir, size=416, boxes=[PLANTED])
    hub = StreamHub()
    return Application(cfg.settings, cfg.paths, stream_hub=hub), hub


@contextmanager
def api_client(app: Application, hub: StreamHub) -> Iterator[tuple[ApiServer, httpx.Client]]:
    """A real uvicorn server on a free port and a real HTTP client: no test-client shortcuts."""
    server = ApiServer(create_app(app, hub), host="127.0.0.1", port=0, logger=get_logger("t"))
    server.start()
    try:
        with httpx.Client(base_url=server.url, timeout=15) as client:
            yield server, client
    finally:
        assert server.stop()


@pytest.fixture
def running(make_config: MakeConfig) -> Iterator[tuple[Application, StreamHub, httpx.Client]]:
    app, hub = build(make_config)
    app.start()
    try:
        assert wait_for(lambda: app.results.latest("synth") is not None), "no frame was analysed"
        with api_client(app, hub) as (_, client):
            yield app, hub, client
    finally:
        app.stop()


# ------------------------------------------------------------------ system


def test_the_viewer_page_is_served_and_loads_nothing_external(running) -> None:  # type: ignore[no-untyped-def]
    _, _, client = running
    page = client.get("/")
    assert page.status_code == 200 and "VIGIL-88" in page.text
    assert "http://" not in page.text.replace("http://www.w3.org", "")  # no external resources
    assert "innerHTML" not in page.text  # data reaches the DOM through textContent only


def test_health_reports_the_real_detector_and_grants_detection(running) -> None:  # type: ignore[no-untyped-def]
    _, _, client = running
    body = client.get("/api/v1/system/health").json()
    assert body["state"] == "running" and body["detector"]["backend"] == "onnxruntime"
    available = {c["capability"] for c in body["capabilities"] if c["available"]}
    assert available == {"detection"}  # and nothing else: tracking, zones, ... stay unavailable
    assert body["detector"]["weights_sha256"]


def test_info_has_version_and_config_hash(running) -> None:  # type: ignore[no-untyped-def]
    _, _, client = running
    body = client.get("/api/v1/system/info").json()
    assert body["version"] and len(body["config_hash"]) == 64 and body["detector"]["name"]


def test_metrics_report_stage_latencies_and_unmeasured_gpu_as_null(running) -> None:  # type: ignore[no-untyped-def]
    app, _, client = running
    assert wait_for(lambda: app.snapshot().system is not None)
    body = client.get("/api/v1/system/metrics").json()
    assert set(body["stage_latency"]) >= {"preprocess", "infer", "postprocess"}
    assert (
        body["inference_latency"]["count"] > 0 and body["inference_latency"]["p50_ms"] is not None
    )
    assert body["counters"]["frames_inferred_total"] > 0
    assert body["counters"]["frames_dropped_total"] == 0
    for field in ("gpu_utilization_percent", "gpu_memory_used_mb"):  # None or a real number
        assert body["system"][field] is None or body["system"][field] >= 0


# ------------------------------------------------------------------ cameras


def test_camera_list_and_detail(running) -> None:  # type: ignore[no-untyped-def]
    _, _, client = running
    (cam,) = client.get("/api/v1/cameras").json()
    assert cam["camera_id"] == "synth" and cam["frames_received"] > 0
    assert client.get("/api/v1/cameras/synth").json()["camera_id"] == "synth"


@pytest.mark.parametrize("bad", ["nope", "NOPE", "a", "../etc", "x" * 60])
def test_unknown_or_malformed_camera_ids_are_404(running, bad: str) -> None:  # type: ignore[no-untyped-def]
    _, _, client = running
    for path in ("", "/detections", "/snapshot.jpg", "/stream.mjpeg"):
        assert client.get(f"/api/v1/cameras/{bad}{path}").status_code == 404


def test_detections_are_in_source_pixels_and_match_the_planted_box(running) -> None:  # type: ignore[no-untyped-def]
    _, _, client = running
    body = client.get("/api/v1/cameras/synth/detections").json()
    assert (body["width_px"], body["height_px"]) == (320, 240)
    (det,) = body["detections"]
    assert det["object_class"] == "person" and det["native_label"] == "person"
    box = det["bbox"]
    assert (box["x1"], box["y1"], box["x2"], box["y2"]) == pytest.approx(EXPECTED, abs=0.6)
    assert body["model"]["backend"] == "onnxruntime"


# ------------------------------------------------------------------ annotations


def decode(jpeg: bytes) -> np.ndarray:  # type: ignore[type-arg]
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)  # type: ignore[no-any-return]


@pytest.fixture
def frozen(
    make_config: MakeConfig,
) -> Iterator[tuple[Application, StreamHub, ApiServer, httpx.Client]]:
    """A camera that has ended on its last frame, so every snapshot is the SAME image."""
    cams = [{"camera_id": "synth", "source": {"kind": "synthetic", "frame_count": 3}}]
    app, hub = build(make_config, cameras=cams)
    app.start()
    try:
        assert app.wait_until_drained(30)
        with api_client(app, hub) as (server, client):
            yield app, hub, server, client
    finally:
        app.stop()


def changed_pixels(annotated: np.ndarray, raw: np.ndarray) -> np.ndarray:  # type: ignore[type-arg]
    """Where the annotated frame differs from the raw one. Rows near the bottom are ignored:
    that is where a STALE banner would appear if the machine were slow."""
    mask = np.abs(annotated.astype(int) - raw.astype(int)).max(axis=2) > 30
    mask[200:, :] = False
    return mask  # type: ignore[no-any-return]


def assert_outlines_planted_box(mask: np.ndarray) -> None:  # type: ignore[type-arg]
    """The changed pixels must be exactly the outline of the planted box: right extent, hollow
    inside, nothing outside. Checked on pixel differences so JPEG chroma smearing of a thin line
    cannot cause a false failure."""
    ys, xs = np.nonzero(mask)
    x1, y1, x2, y2 = EXPECTED
    assert ys.size > 100, "nothing was drawn"
    assert (xs.min(), ys.min(), xs.max(), ys.max()) == pytest.approx((x1, y1, x2, y2), abs=2.5)
    interior = mask[round(y1) + 6 : round(y2) - 5, round(x1) + 6 : round(x2) - 5]
    assert not interior.any(), "the box should be an outline, not a fill"
    outside = mask.copy()
    outside[max(round(y1) - 3, 0) : round(y2) + 4, max(round(x1) - 3, 0) : round(x2) + 4] = False
    assert not outside.any(), "pixels changed outside the detection box"


def test_the_overlay_is_drawn_exactly_where_the_detection_is(frozen) -> None:  # type: ignore[no-untyped-def]
    """Annotation correctness, checked on pixels rather than by eye."""
    _, _, _, client = frozen
    ann = client.get("/api/v1/cameras/synth/snapshot.jpg?overlay=boxes")
    raw = client.get("/api/v1/cameras/synth/snapshot.jpg?overlay=none")
    assert ann.status_code == raw.status_code == 200
    assert ann.content[:2] == raw.content[:2] == b"\xff\xd8"  # JPEG magic
    a, r = decode(ann.content), decode(raw.content)
    assert a.shape == r.shape == (240, 320, 3)
    assert_outlines_planted_box(changed_pixels(a, r))


def test_the_overlay_option_controls_what_is_drawn(frozen) -> None:  # type: ignore[no-untyped-def]
    _, _, _, client = frozen
    sizes = {
        o: len(client.get(f"/api/v1/cameras/synth/snapshot.jpg?overlay={o}").content)
        for o in ("none", "boxes", "boxes,labels,info")
    }
    # the same frame, so more annotation means strictly more detail
    assert sizes["none"] < sizes["boxes"] < sizes["boxes,labels,info"]


@pytest.mark.parametrize("bad", ["circles", "boxes,zebra", "BOXES;DROP"])
def test_unknown_overlay_options_are_rejected_with_the_valid_list(running, bad: str) -> None:  # type: ignore[no-untyped-def]
    _, _, client = running
    for path in ("snapshot.jpg", "stream.mjpeg"):
        r = client.get(f"/api/v1/cameras/synth/{path}?overlay={bad}")
        assert r.status_code == 422 and "boxes" in r.json()["detail"]


def test_overlay_parsing_and_canonical_keys() -> None:
    assert parse_overlay(None).key == "boxes+labels+info"
    assert parse_overlay("none").key == "none" and not parse_overlay("none").draws_anything
    assert parse_overlay("labels, boxes").key == "boxes+labels"  # order does not split streams
    assert parse_overlay("boxes").key != parse_overlay("boxes,labels").key


def test_stream_fps_is_range_checked(running) -> None:  # type: ignore[no-untyped-def]
    _, _, client = running
    assert client.get("/api/v1/cameras/synth/stream.mjpeg?fps=0.01").status_code == 422
    assert client.get("/api/v1/cameras/synth/stream.mjpeg?fps=500").status_code == 422


def test_a_camera_that_has_produced_nothing_gives_404_not_a_blank_image(
    make_config: MakeConfig,
) -> None:
    app, hub = build(
        make_config,
        cameras=[{"camera_id": "idle", "enabled": False, "source": {"kind": "synthetic"}}],
    )
    app.start()
    try:
        with api_client(app, hub) as (_, client):
            for path in ("snapshot.jpg", "detections"):
                r = client.get(f"/api/v1/cameras/idle/{path}")
                assert r.status_code == 404 and "no frame" in r.json()["detail"]
    finally:
        app.stop()


# ------------------------------------------------------------------ the live MJPEG stream


@pytest.fixture
def served(make_config: MakeConfig) -> Iterator[tuple[Application, StreamHub, ApiServer]]:
    app, hub = build(make_config)
    app.start()
    server = ApiServer(create_app(app, hub), host="127.0.0.1", port=0, logger=get_logger("t"))
    server.start()
    try:
        assert wait_for(lambda: app.results.latest("synth") is not None)
        yield app, hub, server
    finally:
        assert server.stop()
        app.stop()


def read_one_part(response: httpx.Response) -> bytes:
    """Read raw chunks until one complete multipart JPEG part has arrived."""
    buf = b""
    for chunk in response.iter_raw():
        buf += chunk
        head_end = buf.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        header = buf[:head_end].decode()
        length = int(
            next(
                line for line in header.split("\r\n") if line.lower().startswith("content-length")
            ).split(":")[1]
        )
        if len(buf) >= head_end + 4 + length:
            return buf[head_end + 4 : head_end + 4 + length]
    raise AssertionError("stream ended before a full frame arrived")


def test_the_mjpeg_stream_delivers_decodable_annotated_frames(frozen) -> None:  # type: ignore[no-untyped-def]
    _, hub, server, client = frozen
    key = StreamKey("synth", "boxes")
    raw = decode(client.get("/api/v1/cameras/synth/snapshot.jpg?overlay=none").content)
    url = f"{server.url}/api/v1/cameras/synth/stream.mjpeg?overlay=boxes"
    with httpx.stream("GET", url, timeout=15) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("multipart/x-mixed-replace; boundary=frame")
        assert r.headers["cache-control"] == "no-store"
        first = read_one_part(r)
        assert wait_for(lambda: hub.viewer_count(key) == 1)  # encoding runs only while watched
    img = decode(first)
    assert img.shape == (240, 320, 3)
    assert_outlines_planted_box(changed_pixels(img, raw))  # the STREAM carries the same overlay
    # the client went away: the viewer slot is released and the stream stops being encoded
    assert wait_for(lambda: hub.viewer_count(key) == 0, timeout=10)
    assert key not in hub.active_keys()


def test_nothing_is_encoded_while_nobody_is_watching(served) -> None:  # type: ignore[no-untyped-def]
    app, hub, _ = served
    time.sleep(0.6)  # several stream-worker periods
    assert hub.active_keys() == () and hub.latest(StreamKey("synth", "boxes")) is None
    assert app.metrics.histogram("vigil_stream_encode_ms").summary().count == 0


def test_viewers_of_the_same_overlay_share_one_encode(served) -> None:  # type: ignore[no-untyped-def]
    app, hub, server = served
    key = StreamKey("synth", "boxes")
    url = f"{server.url}/api/v1/cameras/synth/stream.mjpeg?overlay=boxes"
    with httpx.stream("GET", url, timeout=15) as a, httpx.stream("GET", url, timeout=15) as b:
        read_one_part(a)
        read_one_part(b)
        assert wait_for(lambda: hub.viewer_count(key) == 2)
        assert hub.active_keys().count(key) == 1  # one stream, however many viewers
        before = app.metrics.histogram("vigil_stream_encode_ms").summary().count
        time.sleep(1.0)
        encoded = app.metrics.histogram("vigil_stream_encode_ms").summary().count - before
        stream_fps = app.settings.api.stream_fps
        assert encoded <= stream_fps * 1.0 * 1.5  # one encode per tick, not one per viewer


def test_end_to_end_latency_is_measured_once_someone_is_watching(served) -> None:  # type: ignore[no-untyped-def]
    app, _, server = served
    with httpx.stream("GET", f"{server.url}/api/v1/cameras/synth/stream.mjpeg", timeout=15) as r:
        read_one_part(r)
    # synthetic frames are PACED (replay-clocked), so e2e is correctly NOT reported for them
    assert app.metrics.histogram("vigil_end_to_end_ms", camera="synth").summary().count == 0


def test_the_stream_ends_when_the_application_stops(make_config: MakeConfig) -> None:
    app, hub = build(make_config)
    app.start()
    server = ApiServer(create_app(app, hub), host="127.0.0.1", port=0, logger=get_logger("t"))
    server.start()
    try:
        assert wait_for(lambda: app.results.latest("synth") is not None)
        done = threading.Event()

        def consume() -> None:
            try:
                with httpx.stream(
                    "GET", f"{server.url}/api/v1/cameras/synth/stream.mjpeg", timeout=15
                ) as r:
                    for _ in r.iter_raw():
                        pass
            except httpx.HTTPError:
                pass
            finally:
                done.set()

        threading.Thread(target=consume, daemon=True).start()
        assert wait_for(lambda: hub.viewer_count(StreamKey("synth", "boxes+labels+info")) == 1)
        app.stop()  # closes the hub, which wakes the generator
        assert done.wait(10), "a connected viewer was left hanging after shutdown"
    finally:
        server.stop()


# ------------------------------------------------------------------ security


def test_with_auth_enabled_every_api_route_needs_the_token(make_config: MakeConfig) -> None:
    app, hub = build(make_config, **{"api.auth.enabled": True, "api.auth.token": TOKEN})
    app.start()
    try:
        with api_client(app, hub) as (_, client):
            for path in (
                "/api/v1/system/health",
                "/api/v1/cameras",
                "/api/v1/cameras/synth/detections",
            ):
                assert client.get(path).status_code == 401
                bad = {"Authorization": "Bearer wrong"}
                assert client.get(path, headers=bad).status_code == 401
                basic = {"Authorization": f"Basic {TOKEN}"}  # right secret, wrong scheme
                assert client.get(path, headers=basic).status_code == 401
            good = {"Authorization": f"Bearer {TOKEN}"}
            assert client.get("/api/v1/system/health", headers=good).status_code == 200
            assert client.get("/").status_code == 200  # the static page itself carries no data
    finally:
        app.stop()


def test_without_auth_the_api_is_open_on_loopback(running) -> None:  # type: ignore[no-untyped-def]
    _, _, client = running
    assert client.get("/api/v1/system/health").status_code == 200


def test_the_token_never_appears_in_any_response(make_config: MakeConfig) -> None:
    app, hub = build(make_config, **{"api.auth.enabled": True, "api.auth.token": TOKEN})
    app.start()
    try:
        with api_client(app, hub) as (_, client):
            auth = {"Authorization": f"Bearer {TOKEN}"}
            for path in (
                "/api/v1/system/health",
                "/api/v1/system/info",
                "/api/v1/system/metrics",
                "/api/v1/cameras",
            ):
                assert TOKEN not in client.get(path, headers=auth).text
    finally:
        app.stop()


def test_a_port_that_is_taken_fails_with_a_clear_error(served) -> None:  # type: ignore[no-untyped-def]
    app, hub, server = served
    from vigil.core.errors import CapabilityError

    clash = ApiServer(
        create_app(app, hub), host="127.0.0.1", port=server.port, logger=get_logger("t")
    )
    with pytest.raises(CapabilityError, match="already in use"):
        clash.start()
