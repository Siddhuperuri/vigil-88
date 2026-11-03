# 01. Fresh install on a clean Windows machine

**Claim:** a person who has only Windows can install VIGIL-88 from the installer, start it, and remove it.

## Needs

- A clean Windows 10 or 11 machine or virtual machine: a snapshot that has never had Python, Node, git or
  a checkout of this repository. A virtual machine is fine and is preferred, because it can be reset.
- Signed in as an **ordinary user**, not an administrator. The installer is per-user and must not ask.
- The installer file (`VIGIL-88-DEV-ONLY-Setup-<version>.exe`) and `scripts/verify/clean-install.ps1`, copied
  across. Nothing else from the repository is needed.
- Optional: a webcam passed through to the machine, for step 8.

## Automated part

```powershell
powershell -File clean-install.ps1 -Installer .\VIGIL-88-DEV-ONLY-Setup-0.1.0.exe -Evidence clean-install.json
```

It installs silently, then checks: the program, the engine and the console are installed; there is a
Start-menu entry and an uninstall entry; the application starts the engine it ships with; the engine
listens on loopback only; it reports first-run state; and quitting leaves no engine running. The evidence
file records which development tools it **found** and whether it ran elevated, and says in its verdict
whether the machine counts as clean. A pass on a machine with tools present is reported as not a
clean-machine result.

## By hand (a person at the screen)

1. Double-click the installer. **No administrator prompt may appear.** Windows may show a SmartScreen warning
   because the installer is unsigned (plan decision 3); note it, choose *More info*, *Run anyway*.
2. Finish the installer. Open the Start menu and find *VIGIL-88*.
3. Start it. Within a minute the setup screen appears. No console window, no error.
4. Create the administrator, save the recovery codes, and reach the live wall.
5. Open *System*. Every line says what it measured, or *unavailable*; none is a bare zero.
6. Add the webcam (if present) from *Cameras*, press *Test*, save, and apply. It should come online.
7. Close the window: the application keeps running in the tray. Quit from the tray menu: the engine stops.
8. Restart Windows. The application does **not** start by itself (start-with-Windows is off by default).
9. Uninstall from *Settings, Apps*. Follow [08](08-uninstall.md) for the data prompt.

Record the result with [the template](evidence/TEMPLATE.md).

## What a pass does not show

It does not show the application is safe to give to anyone: the installer is unsigned and the project has no
licence ([RELEASE](../RELEASE.md)). It does not show GPU use; the installer carries the CPU engine only.
