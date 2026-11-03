"""Trying a camera without adding it, and naming the devices a machine has.

`probe_source` opens a source, reads one frame and closes it, and reports what was granted. It runs
on a throwaway thread with a deadline, because `VideoCapture.open` and `read` cannot be cancelled
(ADR 0011): a probe that hangs is **abandoned and reported as timed out**, and its thread is left to
finish or die with the process. That is a leak of one thread per hung probe, bounded by how often a
person presses the button, and it is the honest alternative to freezing the request.

`list_devices` uses a small enumerator that reads the operating system's device list and does
**not** open any camera, so listing never lights an LED or takes a device from the engine.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass

from vigil.core.errors import SourceError, VigilError
from vigil.core.protocols.source import FrameSource
from vigil.domain.camera import SourceSpec
from vigil.ingest.sources.factory import build_source

DEFAULT_TIMEOUT_S = 15.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    message: str
    width_px: int | None = None
    height_px: int | None = None
    fps: float | None = None
    backend: str | None = None
    detail: str | None = None
    timed_out: bool = False


def probe_source(
    spec: SourceSpec,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    build: Callable[[SourceSpec], FrameSource] = build_source,
) -> ProbeResult:
    box: list[ProbeResult] = []

    def work() -> None:
        source: FrameSource | None = None
        try:
            source = build(spec)
            info = source.open()
            frame = source.read()
            if frame is None:
                box.append(ProbeResult(False, "It opened but produced no frame."))
                return
            box.append(
                ProbeResult(
                    True,
                    f"Connected. First frame {frame.pixels.width_px}x{frame.pixels.height_px}.",
                    frame.pixels.width_px,
                    frame.pixels.height_px,
                    info.fps,
                    info.backend,
                    info.detail,
                )
            )
        except SourceError as exc:
            box.append(ProbeResult(False, exc.message))
        except VigilError as exc:
            box.append(ProbeResult(False, exc.message))
        except Exception as exc:  # noqa: BLE001 - a probe must report any failure, never raise
            # Only the type: the text of a driver error can echo a URL, credentials included.
            box.append(
                ProbeResult(False, f"The source failed unexpectedly ({type(exc).__name__}).")
            )
        finally:
            if source is not None:
                with contextlib.suppress(OSError, VigilError):
                    source.close()  # closing after a failure must not mask what the probe found

    thread = threading.Thread(target=work, name="vigil-probe", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return ProbeResult(
            False,
            f"No answer within {timeout_s:g} s. The probe was abandoned; the device or network "
            "may be stuck.",
            timed_out=True,
        )
    return box[0] if box else ProbeResult(False, "The probe ended without a result.")


@dataclass(frozen=True, slots=True)
class Device:
    index: int
    name: str
    backend: str


@dataclass(frozen=True, slots=True)
class DeviceList:
    devices: tuple[Device, ...]
    available: bool
    reason: str | None


def list_devices() -> DeviceList:
    try:
        import cv2
        from cv2_enumerate_cameras import enumerate_cameras
    except ImportError:
        return DeviceList((), False, "Camera names need the capture extra: uv sync --extra capture")
    found: dict[tuple[int, str], Device] = {}
    for api, label in ((cv2.CAP_DSHOW, "dshow"), (cv2.CAP_MSMF, "msmf")):
        for cam in enumerate_cameras(api):
            found.setdefault((cam.index, cam.name), Device(cam.index, cam.name, label))
    return DeviceList(tuple(sorted(found.values(), key=lambda d: (d.index, d.name))), True, None)
