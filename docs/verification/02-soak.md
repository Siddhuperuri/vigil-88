# 02. Eight-hour soak of the whole application

**Claim:** left running and used, the application does not leak memory or handles, and the console and
engine stay responsive.

The earlier soak (`scripts/soak.py`) measured the **engine process alone**. This one measures what a person
runs: the Electron shell, its renderer, GPU and utility processes, the console, and the engine.

## Needs

- A machine that can be left alone for eight hours, with sleep and screen-lock disabled, plugged in.
- The repository (for the source-tree run) or an installed `VIGIL-88.exe` (for the installed run).
- Optional: real cameras in the configuration. The harness uses two synthetic cameras, which exercise the
  pipeline but not a real decoder or network.

## Run

```powershell
powershell -File scripts/verify/soak-app.ps1 -Minutes 480
powershell -File scripts/verify/soak-app.ps1 -Minutes 480 -Packaged "<path to the installed VIGIL-88.exe>"
```

What it does, for as long as it is told:

- `desktop/e2e/soak.spec.ts` starts the application, creates an administrator, then **once a minute visits every
  view** and checks each shows its heading, reads `/system/health`, and checks the live link is up. It counts
  console errors and page errors.
- `scripts/verify/proc_sampler.py` follows the application's process tree and records, every 15 s, memory,
  handles, threads and CPU, **per role** (shell, renderer, GPU, utility, engine).
- The result is `build/soak/<time>/report.md`, `samples.csv`, `journey.json` and `summary.json`.

## How it judges

- The first two minutes are start-up and are not counted as growth.
- Memory, handles and each major role are compared between the **start of the settled period and its end**;
  more than 10% growth is *not shown to be bounded*. Thread counts are reported but do not fail the run.
- A view that does not appear, a health level of *failed*, a dropped live link, or **any** console error
  fails the journey.
- The report states the minutes it ran against the 480 the plan asks for. A short run says it cannot show
  that a long one would pass.

## What it does not cover

Real cameras, a real detection model, GPU memory, the network, Windows sleep and resume, and a person who
interacts for hours. Those need a longer, staffed run recorded with [the template](evidence/TEMPLATE.md).

## Status

The harness was proven on a short run (see [evidence](evidence/)). **The eight-hour run has not been done.**
