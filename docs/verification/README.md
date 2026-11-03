# Verification

This folder says how each claim about VIGIL-88 is checked, what the check needs, and **whether it has
been done**. A check that needs hardware or a machine this repository does not have is written up as a
procedure with a script and an evidence template, and is marked **not run**. Nothing here is a pass
because a script exists.

## Where things stand

| Area | Procedure | Harness | Status |
|---|---|---|---|
| Fresh install on a clean Windows machine | [01](01-clean-install.md) | `scripts/verify/clean-install.ps1` | **Not run on a clean machine.** The script ran on the development machine, which is not one (see [evidence](evidence/)) |
| 8-hour soak of the whole application | [02](02-soak.md) | `scripts/verify/soak-app.ps1`, `desktop/e2e/soak.spec.ts`, `scripts/verify/proc_sampler.py` | **8 h not run.** A short run proved the harness works (see [evidence](evidence/)) |
| Disk full | [03](03-failure-drills.md) | `tests/integration/test_failure_drills.py` | **Simulated fault passes in CI.** A real full volume was not tried |
| Camera unplugged, then recovery | [03](03-failure-drills.md) | `tests/integration/test_failure_drills.py` | **Simulated fault passes in CI.** A real unplugged camera was not tried |
| Corrupted config, missing model, unreachable camera | [03](03-failure-drills.md) | `desktop/e2e/drills.spec.ts` | Automated, passes |
| GPU driver reset | [03](03-failure-drills.md) | none | **Not drillable** here; there is no GPU runtime in the installer |
| Real RTSP camera | [04](04-rtsp-camera.md) | `scripts/verify/rtsp_check.py` | **Not run.** No camera was available |
| Real NVIDIA GPU | [05](05-gpu.md) | `scripts/verify/gpu_check.py` | **Environment read only** (`--dry-run`); the benchmark was not run |
| NVDA screen reader | [06](06-nvda.md) | checklist | **Not run.** Needs NVDA and a person |
| Antivirus interaction | [07](07-antivirus.md) | `scripts/verify/av-check.ps1` | **Defender on the development machine only** (see [evidence](evidence/)) |
| Uninstall, both data branches | [08](08-uninstall.md) | `scripts/verify/uninstall-branches.ps1` | Both branches driven on the development machine (see [evidence](evidence/)) |

## Rules for evidence

1. An evidence file records **what was measured, on what, when and for how long**. It does not record a
   conclusion the measurement does not support.
2. A run on the development machine is labelled as such. It never stands in for a clean machine.
3. A short run is labelled short. The soak report states the minutes it ran against the 480 the plan asks for.
4. No evidence file contains a password, an address with credentials, or a path that names a person.
5. When a check fails, the failing evidence is kept and the failure is fixed or recorded in
   [LIMITATIONS](../LIMITATIONS.md); the check is not loosened.

Use [evidence/TEMPLATE.md](evidence/TEMPLATE.md) for anything done by hand.

## Evidence kept here

| File | What it is |
|---|---|
| [uninstall-branches.json](evidence/uninstall-branches.json) | Both uninstall branches on the development machine |
| [antivirus-defender.json](evidence/antivirus-defender.json) | Microsoft Defender scan and a 30 s run, one machine |
| [clean-install-dev-machine.json](evidence/clean-install-dev-machine.json) | The install check; it states it is **not** a clean machine |
| [drills-automated.json](evidence/drills-automated.json) | The automated drills |
| [soak-6min-installed-app.md](evidence/soak-6min-installed-app.md) | A 6-minute whole-application soak |
| [gpu-environment.json](evidence/gpu-environment.json) | The card, driver and execution providers; no benchmark |
