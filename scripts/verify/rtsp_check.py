"""Measures a real network camera over RTSP, and records what it did when the network broke.

    python scripts/verify/rtsp_check.py <rtsp address> [--seconds 60] [--credential NAME]
                                       [--break-network] [--out evidence.json]

It needs a real camera, so it cannot run in CI and has not been run against one. What it does:

  * opens the stream through the same source class the engine uses and times the first frame;
  * reads for `--seconds`, measuring the frame rate actually delivered and the picture size;
  * with `--break-network`, asks the person to interrupt the camera's network for about ten seconds
    and restore it, and measures how long reconnecting took to bring frames back;
  * writes an evidence file. The address is recorded WITHOUT its credentials.

Credentials come from the stored credential named by `--credential` (see Cameras), never from the
command line. Nothing here claims the camera works: it reports what it measured.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from vigil.config.schema.camera import CameraConfig
from vigil.core.clock import SystemClock
from vigil.core.errors import SourceError
from vigil.ingest.sources.factory import build_source
from vigil.pipeline.cameras import camera_from_config
from vigil.version import __version__

OUTAGE_PROMPT_S = 10
GIVE_UP_S = 60


def address_without_credentials(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def summarise(
    frames: int, seconds: float, outages: list[dict[str, object]], first_frame_s: float | None
) -> dict[str, object]:
    """The facts, and a verdict that only says what was shown."""
    fps = frames / seconds if seconds > 0 else 0.0
    recovered = [o for o in outages if o["recovered"]]
    if first_frame_s is None:
        verdict = "NO FRAME: the stream did not deliver a picture."
    elif outages and len(recovered) != len(outages):
        verdict = "FAILED TO RECOVER: frames did not return after the network was restored."
    elif outages:
        slowest = max(float(str(o["seconds_to_recover"])) for o in recovered)
        verdict = f"RECOVERED {len(recovered)} outage(s); slowest took {slowest:.1f} s."
    else:
        verdict = "Delivered frames. Reconnect behaviour was NOT exercised in this run."
    return {
        "frames": frames,
        "seconds": round(seconds, 1),
        "measured_fps": round(fps, 2),
        "first_frame_s": None if first_frame_s is None else round(first_frame_s, 2),
        "outages": outages,
        "verdict": verdict,
    }


def run(url: str, seconds: float, credential: str | None, break_network: bool) -> dict[str, object]:
    camera = camera_from_config(
        CameraConfig.model_validate(
            {
                "camera_id": "verify",
                "source": {
                    "kind": "rtsp",
                    "url": url,
                    **({"credential_ref": credential} if credential else {}),
                },
            }
        )
    )
    source = build_source(camera.source, SystemClock())
    began = time.monotonic()
    size: str | None = None
    first_frame_s: float | None = None
    frames = 0
    outages: list[dict[str, object]] = []
    outage_started: float | None = None
    prompted = False
    try:
        source.open()
    except SourceError as exc:
        return {
            **summarise(0, 0.0, [], None),
            "error": exc.message,
            "address": address_without_credentials(url),
        }
    while time.monotonic() - began < seconds:
        now = time.monotonic()
        if break_network and not prompted and now - began > seconds / 3:
            prompted = True
            print(
                f"\n>>> Interrupt the camera's network now (about {OUTAGE_PROMPT_S} s), "
                "then restore it.\n"
            )
        try:
            frame = source.read()
        except SourceError:
            frame = None
        if frame is None:
            if outage_started is None:
                outage_started = now
            try:  # the engine reopens on a failed read; do the same, with a pause
                source.close()
                time.sleep(1.0)
                source.open()
            except SourceError:
                pass
            if now - outage_started > GIVE_UP_S:
                break
            continue
        frames += 1
        if first_frame_s is None:
            first_frame_s = now - began
            size = f"{frame.pixels.width_px}x{frame.pixels.height_px}"
        if outage_started is not None:
            outages.append(
                {
                    "started_s": round(outage_started - began, 1),
                    "recovered": True,
                    "seconds_to_recover": round(now - outage_started, 1),
                }
            )
            outage_started = None
    if outage_started is not None:
        outages.append({"started_s": round(outage_started - began, 1), "recovered": False})
    source.close()
    result = summarise(frames, time.monotonic() - began, outages, first_frame_s)
    return {**result, "picture": size, "address": address_without_credentials(url)}


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    seconds = float(argv[argv.index("--seconds") + 1]) if "--seconds" in argv else 60.0
    credential = argv[argv.index("--credential") + 1] if "--credential" in argv else None
    evidence = run(argv[0], seconds, credential, "--break-network" in argv)
    evidence["vigil_version"] = __version__
    evidence["measured_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    text = json.dumps(evidence, indent=2, sort_keys=True)
    print(text)
    if "--out" in argv:
        Path(argv[argv.index("--out") + 1]).write_text(text, encoding="utf-8")
    verdict = str(evidence["verdict"])
    return 0 if verdict.startswith(("RECOVERED", "Delivered")) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
