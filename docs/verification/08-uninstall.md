# 08. Uninstall, and what happens to the data

The uninstaller offers to keep or remove the person's data (cameras, zones, incidents, evidence, accounts).
The default, including for a silent uninstall, is to **keep** it. An uninstaller should not delete evidence
unasked.

| How it is run | What happens to the data |
|---|---|
| Interactively, answer **No** (the default button) | Kept |
| Interactively, answer **Yes** | Removed. The prompt says this cannot be undone |
| `Uninstall VIGIL-88.exe /S` (silent) | Kept |
| `Uninstall VIGIL-88.exe /S /REMOVEDATA` | Removed, without a prompt. For scripts and administrators; `/S` alone never implies it |

## Automated: both branches

```powershell
powershell -File scripts/verify/uninstall-branches.ps1 -Evidence uninstall.json
```

It installs the development installer silently into a throwaway folder (so it cannot touch a real install),
plants a marker in the per-user data folder, uninstalls with `/S` and checks the marker survives and the program
is gone; then installs again, plants a marker, uninstalls with `/S /REMOVEDATA`, and checks the data folder is
gone. **It refuses to run if the data folder already holds anything**, because one of its tests deletes that
folder. Run it on a machine or account that has never used the application.

## By hand

The two interactive answers are not scripted, because they are a message box.

1. Install, run the application, create an administrator and a camera, quit.
2. *Settings, Apps, VIGIL-88, Uninstall*. At the prompt choose **No**.
3. Confirm the program is gone, the folder under `%APPDATA%` is still there, and a fresh install finds the
   camera and the account.
4. Uninstall again and choose **Yes**. Confirm the `%APPDATA%\VIGIL-88` folder is gone.
5. Confirm that, in both cases, no `vigil-engine.exe` is left running and the Start-menu entry is gone.
6. Upgrade-in-place: install the same version over itself with the app closed. The data must be untouched.

Record the by-hand steps with [the template](evidence/TEMPLATE.md).
