# D2 report: cameras and zones

**Branch:** `feat/d0-foundation` (D0, D1 and D2 together) · nothing pushed or merged.

## The deviation, first

The plan said D2 would add a camera manager so a camera could be added and a zone drawn **with no
restart**. That is **not built**. Reading the pipeline showed per-camera state is created once and shared
by the scheduler, the inference worker, the analysis assembly and the response stack; making it live is a
rewrite of the assembly, which is the stop-and-ask case in the brief and the part of the system where a
subtle bug silences a camera. [ADR 0017](adr/0017-apply-configuration-by-controlled-restart.md) records
the alternative that was built: **a change is saved at once and takes effect after a controlled engine
restart**, and the console says so before the person agrees. Two exit criteria ("a camera added with no
restart", "a zone taking effect on a camera that started without one") are therefore **unmet**.

**Cost of that choice:** applying changes stops monitoring of every camera for the length of an engine
start. Measured in the real app (Electron, real engine, a video-file camera, no detection model to load):
**about 3.5 s** from clicking Restart to the camera reading ONLINE (3.4 to 3.6 s over four runs). With a
model to load it will be longer; that was not measured.

## What was built

| Area | What | Verified by |
|---|---|---|
| Sources | Video file (paced, optional loop, seek fix), RTSP (OpenCV FFmpeg over TCP, open and read timeouts), device listing, a bounded connection probe that is abandoned at its deadline | `test_video_and_rtsp.py`, `test_probe.py` |
| Credentials | Windows Credential Manager through `keyring`, or named environment variables. Never in camera files, logs, errors or the audit log; addresses are redacted of userinfo and query | `test_video_and_rtsp.py`, `test_config_admin.py` |
| Config writer | Atomic camera and zone files written through the same models the loader uses, so a saved file is one the engine accepts | `tests/unit/config/test_writer.py` |
| Admin API | Cameras, zones, devices, test, credentials, pending, apply; every route declares one permission, enforced by the all-routes test | `test_config_admin.py` |
| Apply | `GET /config/pending` is computed from files against the running settings, so it is true after a crash or a hand edit. `POST /config/apply` asks the supervisor to relaunch; without a supervisor it refuses (409) | `test_config_admin.py`, supervisor tests, e2e |
| Cameras screen | List with live state and the saved-but-not-applied state in words; add and edit with a connection test; write-only username and password (sent once, cleared after sending); remove behind a confirmation whose safe choice is first | `Cameras.test.tsx`, e2e |
| Pending bar | Names each waiting change, states that every camera pauses, defaults to not restarting, shows the restart until the engine is back, shows why a restart failed | `Cameras.test.tsx`, e2e |
| Zone editor | Polygons on the camera's current frame. Drag a point, or arrow keys (Shift for larger steps, Delete to remove), or typed numbers in a table, so dragging is never the only way (WCAG 2.5.7). Client checks mirror the engine; Save is off with the reason; the engine's reasons are shown if it refuses | `Zones.test.tsx`, `geometry.test.ts`, e2e |
| Desktop | After an engine restart the window returns to the console view it was on, not the first one | e2e |

## Verified in the real app

A ninth Playwright test drives the whole journey in real Electron against the real engine: create the
administrator, open Cameras, be refused a bad name in place, add a video-file camera made on the spot with
OpenCV, **test the connection** (the engine opened it and read a 320x240 frame), save, see it listed as
"Saved. Starts when monitoring restarts." and the bar naming one waiting change, open the apply dialog and
decline (nothing changes), apply, see "Restarting monitoring…", come back on the same view still signed in
with the camera ONLINE and nothing pending and a new engine process, then draw a zone, move a point with the
keyboard, save, and see the zone and the camera's `zones_ref` listed as two waiting changes. axe at WCAG 2.2
AA with colour contrast is clean on the add form and the zone editor.

## Defects found on the way

- After an apply the camera list kept showing the pre-restart state: the real-app test caught that the list
  was fetched once. It now re-reads when the pending state is re-read, and shows state from the live channel.
- After an apply the window loaded the console's first view, losing the person's place. The shell now
  restores the view.
- The test engine's config lacked the `cameras/*.yaml` include, so a saved camera was not loaded after a
  restart; this was a test-setup fault, and a reminder that a hand-written `vigil.yaml` without that include
  will not pick up console-made cameras.
- Running `vigil run --source rtsp://127.0.0.1:9/s` ended with "shutdown INCOMPLETE": the capture worker was
  blocked in the camera open (8 s) past the 5 s shutdown budget. The open and read timeouts are now 4 s, a test
  holds them under the budget, and the same command shuts down clean in 3.7 s. The full gate had also caught
  nine older tests that still asserted RTSP and video files were "not implemented"; they now say what is true.
- Dialogs: a native `<dialog>` focuses its first control, so the safe choice ("Keep it", "Not now") is first
  in the DOM. `autoFocus` is forbidden by the accessibility lint and was not used.

## Not built, and said so on screen or in LIMITATIONS

Users and roles screens, the audit log screen, the analytics and report screen (all three have working
APIs), the diagnostics bundle, settings and configuration import/export, alert rules and quiet hours, the
degradation ladder, annotating the pre-roll gap after a reconnect, the full first-run wizard, engine worker
restart. This was a deliberate cut under "finish quickly", not an oversight.

## Not verified

- **RTSP has never run against a real camera.** Everything about it is tested with fakes. Treat it as
  unproven.
- **The real-GPU spike was not run** (the call was declined). The restart time above has no model in it.
- **No screen-reader pass.** The zone editor's keyboard path is exercised by tests and by the real app, but
  nobody has listened to it in NVDA. The D1 and D2 manual accessibility exit criteria are **not met**.
- **Stored credentials were not exercised against Windows Credential Manager in the real app**, only the
  `keyring` boundary with a fake backend.
- **The live-webcam recheck of the D0 fixes (F7, F8) and the `Secure` cookie on the loopback origin remain
  outstanding.**
