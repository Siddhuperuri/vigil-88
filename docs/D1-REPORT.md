# D1 report: core desktop

**Branch:** `feat/d0-foundation` (D0 and D1 together) · nothing pushed or merged.

## What was built

| Area | What | Verified by |
|---|---|---|
| Live channel | `GET /api/v1/telemetry` WebSocket: coalesced snapshot plus a heartbeat, and incident new/updated notices. Upgrade needs a session that may VIEW and a matching Origin; the socket closes with 4401 (session ended) or 4423 (locked) | `tests/unit/api/test_telemetry_api.py`, 7 tests over a real socket |
| Incidents | Operator **resolve** through RESOLVING under the user's name; search already existed | `test_lifecycle.py`; Viewer refused on the new route by the all-routes walk |
| System | Log tail (bounded, administrator only) and storage health (sizes, never a path) | `test_system_ops.py` |
| Console | Shell with status strip, rail, skip link, shortcut sheet; setup with one-time recovery codes; sign-in with generic failure and counted lockout; lock screen; live wall (layouts, three overlays, expand, full screen, snapshot); incident desk; system view | 95 component tests, coverage 94.9% statements / 87.2% branches |
| Triage | `j` `k` `a` `d` `e` `r` `n` `/` `?`; terminal actions ask for a reason and say what they do; a forbidden action is disabled with the engine's reason | `Incidents.test.tsx` |
| Evidence | Integrity result shown per file, with a loud mismatch state; images and clips load through the guarded route | `Incidents.test.tsx` |
| Explanation | Factors add up to the score on screen; checks, caveats, and the "score, not probability" line | `Incidents.test.tsx` |
| Desktop | Tray with state colour and words, hide-to-tray on close, quit from the tray stops the engine; native notification for HIGH and CRITICAL (re-validated in the main process); a generated alert tone with a switch | `notify.test.ts`, `alerts.test.tsx`, e2e |
| Announcements | An assertive live region announces a new HIGH or CRITICAL once | `App.test.tsx` |

## Verified in the real app (Playwright, real Electron, real engine)

Eight tests, including the whole first run in Chromium: create the administrator, see the recovery
codes (the way on is blocked until they are confirmed stored), the live wall with the strip reading
LIVE, the incident desk, the system view, the shortcut sheet, lock, a wrong password refused in
place, unlock, sign out (the engine ends the session), a wrong password with the generic message,
sign in again and land on the view that was open. axe at WCAG 2.2 AA, colour contrast included, is
clean on setup, recovery codes, wall, desk, system, lock and sign-in. Closing the window hides it
with the engine still running; quitting leaves no engine process.

## Defects found on the way

- Route parsing did not decode escaped segments, so `#/wall/cam%201` named a camera that did not exist.
- An empty log at the chosen level showed nothing at all, not even "no records".
- Two of my own mock-routing and layout mistakes were found by the tests, not by inspection.

## Not built, and said so on screen or in LIMITATIONS

Track, trail and zone overlays; notifications while the console is locked; engine worker restart;
camera and zone management (D2); a command palette (D3); a manual theme switch.

## Not verified

- **No screen-reader pass.** Automated checks find a minority of accessibility defects. The D1 exit
  criterion for a manual NVDA pass is **not met**.
- **No incident was seen arriving live in the real app.** The engine has no camera in the end-to-end
  setup, so the desk is verified with seeded incidents in the API and component tests, not with a
  detection. No demo data is shipped or shown.
- **The sound and the native toast were not heard or seen.** The plan and the request sent to the
  shell are tested; whether Windows displayed a toast was not observed.
- Screenshots in light and dark at 200% zoom were taken once and inspected by eye on the setup and
  system screens, with no horizontal overflow at 200%. They are not committed.
- The live-camera recheck of the D0 fixes is still outstanding.
