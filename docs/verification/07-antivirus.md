# 07. Antivirus interaction

**Concern:** a PyInstaller-frozen Python program is a well-known source of false positives, and an unsigned
installer draws SmartScreen warnings. Both would stop a real user.

## What can be checked here

```powershell
powershell -File scripts/verify/av-check.ps1 -Evidence av.json
```

It uses Windows Security (Microsoft Defender) on the machine it runs on. It reports the engine and signature
version, scans the installer and the unpacked application, **runs the application for 30 seconds** with
real-time protection on, and lists any detection. It also reports whether the scanned folder was excluded
from scanning (a scan of an excluded folder proves nothing) and the installer's signature status.

A pass means: **Microsoft Defender, with those signatures, on that machine, raised nothing.**

## What it cannot show

- Any other product. Test the installer and the engine with the products your users run: the usual
  commercial suites, and a second opinion. Upload the installer to a multi-engine scanning service **only if
  you are willing to share it**; once uploaded it is shared with that service's partners.
- SmartScreen. The warning appears when a file carrying the web's origin mark runs; it depends on how the file
  arrived and on reputation, which an unsigned file does not have. Download the installer in a browser, run
  it, and photograph what Windows shows.
- Cloud-based reputation that changes day to day.

## If something is detected

Keep the detection name and the engine's description in the evidence. Report it to the vendor as a false
positive with the file hash. Do not weaken the build to hide it. The usual remedies are a code-signing
certificate (decision 3), building the engine's bootloader from source, or `--onedir` instead of `--onefile`
(already the case; see ADR 0019).

Record by-hand checks with [the template](evidence/TEMPLATE.md).
