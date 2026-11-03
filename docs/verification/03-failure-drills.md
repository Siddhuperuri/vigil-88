# 03. Failure drills

A drill breaks one thing on purpose and checks the application tells the truth, stays up, and recovers. Each
drill has an automated form (a simulated fault) and, where it matters, a manual form (the real fault).

Run every automated drill and get one evidence file:

```powershell
powershell -File scripts/verify/run-drills.ps1 -Evidence drills.json
```

| Drill | Automated | What it proves | What it does not |
|---|---|---|---|
| Corrupted configuration | `desktop/e2e/drills.spec.ts` | Named on screen with the parser's words and the line; no engine left half-configured; mending the file recovers without restarting the app | |
| Missing model | `desktop/e2e/drills.spec.ts` | Engine stops with the command that fixes it and does not pretend to detect | |
| Unreachable camera | `desktop/e2e/drills.spec.ts` | Shown as not working, never ONLINE; engine stays up; quitting does not hang | |
| Disk full | `tests/integration/test_failure_drills.py` | The incident is still recorded; the evidence is reported missing, with the reason, and no file pretends otherwise; the engine keeps running; once there is room, evidence writes again with no restart | A real full volume (the test makes the volume *report* nearly full) |
| Camera unplugged, then recovered | `tests/integration/test_failure_drills.py` | Shown as not online with a reason; the engine stays up and does not treat it as overload; it comes back by itself with a new stream; the interruption is recorded; the footage across the hole is marked | A real USB or network camera |
| GPU driver reset | none | | The installer has no GPU runtime. Drill it once decision 2 is made and the runtime ships |

## Manual: a real full volume

Needs administrator rights, so it is not scripted. On a **test machine**:

1. Create a small virtual disk (Disk Management, *Create VHD*, 600 MB, fixed) and mount it as a drive.
2. Point the data folder at it (`paths.data_dir`) and start the application with a camera and a zone.
3. Fill the drive with a large file until under 100 MB is free.
4. Trigger an incident (walk into the zone).
5. **Expect:** the incident appears on the desk; its evidence says *unavailable* or *partial* with a reason;
   *System, Storage* shows the low space; the live wall keeps running; no crash in the log.
6. Delete the large file. **Expect:** the next incident has evidence again, with no restart.

## Manual: a real unplugged camera

1. Run with a USB webcam. Confirm it is ONLINE on the wall.
2. Pull the cable. **Expect** within seconds: the camera shows RECONNECTING or FAILED with a reason, never ONLINE;
   the engine health stays *running*; other cameras are unaffected.
3. Plug it back in. **Expect:** ONLINE again without anyone pressing anything, and *System* shows a new stream.
4. If an incident was open across the gap, open it: *Evidence* must say the recording has a gap.

Record both with [the template](evidence/TEMPLATE.md).
