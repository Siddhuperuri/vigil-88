# 04. A real RTSP camera

**Claim:** a network camera that speaks RTSP can be added, delivers frames at its real rate, and recovers
when its network path breaks.

Everything about RTSP so far was tested against fakes and against a refused connection. **No real camera has
been tried.** That is the biggest gap in the ingest path.

## Needs

- A network camera or NVR stream reachable from the machine, with its RTSP address and credentials.
- The credentials stored through the console (*Cameras*) under a name. They are never typed on a command line.
- The ability to interrupt the camera's network for ten seconds (unplug its cable, or switch its port off).

## Run

```powershell
uv run python scripts/verify/rtsp_check.py rtsp://<camera address>/<path> --credential <stored name> `
    --seconds 120 --break-network --out rtsp-evidence.json
```

It opens the stream through the same source class the engine uses, times the first frame, measures the real
frame rate and picture size for the duration, and halfway through tells you to break the network. It then
measures how long the engine's reconnect logic takes to bring frames back. The address is written **without**
its credentials.

## By hand, in the application

1. Add the camera in *Cameras*. Press *Test*: it should report the picture size.
2. Apply. The wall shows it ONLINE with a frame rate close to the camera's.
3. Break the network. **Expect** RECONNECTING within the stall timeout, with a reason; never ONLINE.
4. Restore it. **Expect** ONLINE again by itself. *System* shows a new stream.
5. Leave it an hour. Frame rate and memory should be steady (see [02](02-soak.md)).
6. Quit the application while the camera is unreachable. It must stop within five seconds
   (open and read timeouts are four).

## Things worth looking for

H.264 and H.265 streams, a camera that needs TCP rather than UDP, a stream that sends only key frames at
first, an address with special characters in the password, and a camera that disconnects after a day.
Record each that is tried. The check does not choose a transport: if the camera needs TCP and the default
is UDP, the evidence says no frame arrived, and that is a finding.

Record the result with [the template](evidence/TEMPLATE.md). The harness's arithmetic is unit-tested
(`tests/unit/test_verify_scripts.py`); its behaviour against a camera is not, because there is none.
