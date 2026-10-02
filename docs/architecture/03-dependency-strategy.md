# 03 — Dependency Strategy

> Status: DESIGN. No dependencies installed yet.

## 1. Rules

1. **Every dependency is a liability.** Each one is justified in this document, by name. An
   unjustified entry in `pyproject.toml` is a review blocker.
2. **Lock everything, commit the lock.** `uv.lock` for Python, `package-lock.json` for the
   console. A build that cannot be reproduced cannot be debugged.
3. **Pin floors, not ceilings, for libraries; pin exactly for the inference stack.** Torch
   and its CUDA wheels are pinned to an exact version because a silent minor bump can
   change kernel selection, numerics, and VRAM behaviour.
4. **The heavy stack is optional.** `pip install vigil-88` must succeed with no GPU, no
   CUDA, and no torch, and `vigil doctor` must then explain precisely what is missing and
   what still works. Tests for `domain/`, `core/`, `temporal/`, `verification/`, and
   `severity/` must run in that bare environment.
5. **No dependency may appear in `domain/` or `core/`.** Enforced by `import-linter`
   ([02-directory-structure.md](02-directory-structure.md) §5).
6. **Prefer the stdlib.** `pathlib`, `dataclasses`, `enum`, `queue`, `threading`,
   `sqlite3`, `json`, `csv`, `hashlib`, `statistics`, `collections.deque` cover a
   surprising amount of this system. A dependency that saves fifteen lines is not worth a
   supply-chain surface.

## 2. Toolchain

| Concern | Choice | Why |
|---------|--------|-----|
| Python | 3.11.9 (installed, pinned by `.python-version`) | D-001 |
| Package manager | `uv` 0.12.3 (installed) | D-009. Resolves and installs the torch stack in seconds, handles extra indexes properly, PEP 621 native |
| Build backend | `hatchling` | Minimal, PEP 621 native, no plugin ceremony |
| Task runner | PowerShell scripts in `scripts/` | Windows-first. A cross-platform task runner is a dependency with no payoff for a single-target project |
| Console toolchain | Node 20.20.2, npm 10.8.2, Vite 5 | Installed; Vite is the fast, boring default |

## 3. Python dependency groups

Declared as PEP 735 dependency groups plus optional extras, so each install profile is
explicit.

### 3.1 `core` — the base install, no GPU, no CV

| Package | Purpose | Notes |
|---------|---------|-------|
| `pydantic` >=2.9 | Domain validation at boundaries, config schema | v2 is Rust-backed and fast enough for per-request use; still not used on the per-frame hot path |
| `pydantic-settings` >=2.5 | Layered config loading | Gives us env + dotenv + nested models for free |
| `structlog` >=24.4 | Structured logging | Context binding is the feature; stdlib `logging` cannot do it without homegrown adapters |
| `typer` >=0.12 | CLI | Type hints become the CLI contract; pulls `click` |
| `rich` >=13.8 | Dev console rendering, `vigil doctor` output | Already a transitive dep of typer |
| `sqlalchemy` >=2.0 | Typed ORM over SQLite | 2.0 `Mapped[...]` style is genuinely type-safe. Plain `sqlite3` would mean hand-rolling migrations and query composition |
| `alembic` >=1.13 | Schema migrations | A monitoring system accumulates data you cannot throw away on every schema change |
| `python-ulid` >=2.7 | Sortable identifiers | 01 §7.7 |
| `psutil` >=6.0 | CPU, memory, process metrics | No reasonable stdlib equivalent on Windows |
| `pyyaml` >=6.0 | Config and zone files | YAML over TOML for config because zone polygons and nested module params are far more readable |

### 3.2 `vision` — extra, GPU path

| Package | Purpose | Notes |
|---------|---------|-------|
| `torch` (exact pin) | Inference runtime | Installed from the CUDA wheel index, §4 |
| `torchvision` (matching pin) | NMS, ops, transforms | Version must match torch exactly |
| `ultralytics` >=8.3 | YOLO detector backend | **AGPL-3.0 — see §6** |
| `opencv-contrib-python-headless` >=4.10 | Capture, decode, resize, JPEG encode | Headless: no GUI bindings, which we must never use (D-002). `contrib` for the extra trackers and calibration utilities |
| `numpy` >=1.26,<3 | Array interchange | Pinned below 3 until torch and OpenCV both declare support |
| `pynvml` >=11.5 | GPU utilisation, VRAM, temperature | Needed for the degradation ladder and the System view |

### 3.3 `onnx` — extra, alternative/CPU inference path

| Package | Purpose |
|---------|---------|
| `onnxruntime-gpu` or `onnxruntime` | Second backend; the licence-clean deployment path (§6) and the CPU fallback |

### 3.4 `api` — extra, the console backend

| Package | Purpose | Notes |
|---------|---------|-------|
| `fastapi` >=0.115 | HTTP + WS, OpenAPI generation | OpenAPI is what generates the console's TypeScript types; this is load-bearing for type safety across the boundary |
| `uvicorn[standard]` >=0.30 | ASGI server | `[standard]` brings `httptools` and `websockets` |

### 3.5 `dev`

| Package | Purpose |
|---------|---------|
| `pytest` >=8.3, `pytest-cov`, `pytest-timeout` | Test runner, coverage gate, hang protection in CI |
| `hypothesis` >=6.112 | Property tests for the tracker and accumulator (09 §6) |
| `ruff` >=0.6 | Lint + format; replaces black, isort, flake8, pyupgrade, and a dozen plugins |
| `mypy` >=1.11 | Strict typing gate |
| `import-linter` >=2.0 | Layer contracts (02 §5) |
| `pre-commit` >=3.8 | Runs the fast gates before a commit exists |
| `types-pyyaml`, `types-psutil` | Stubs |

### 3.6 Install profiles

```powershell
uv sync --group dev                              # logic work; no GPU, no torch
uv sync --group dev --extra api                  # plus the console backend
uv sync --group dev --extra api --extra vision   # full workstation install
uv sync --no-dev --extra api --extra vision      # runtime
```

## 4. The torch / CUDA problem

This is the one genuinely awkward dependency, and it needs an explicit policy rather than
a hope.

- PyPI's default `torch` wheel on Windows is **CPU-only**. Installing it and expecting CUDA
  is the single most common setup failure in this ecosystem.
- CUDA builds come from PyTorch's own index (`https://download.pytorch.org/whl/<cuXXX>`),
  selected per CUDA runtime version.
- The machine has driver **616.56** and **no CUDA toolkit**. The toolkit is not needed: the
  wheels bundle the CUDA runtime. Only the driver must be new enough for the chosen runtime,
  and this driver is far newer than any currently-shipping wheel requires.

**Policy.**

1. `pyproject.toml` declares an explicit `[tool.uv.sources]` entry pinning `torch` and
   `torchvision` to a named CUDA index, with a CPU index as a named alternative. The exact
   `cuXXX` tag is confirmed against the PyTorch install matrix at the start of P1 and
   recorded here with the date it was verified — not guessed now.
2. `vigil doctor` reports, in this order: torch present, `torch.version.cuda`,
   `torch.cuda.is_available()`, device name, total/free VRAM, and the chosen device. If
   torch is present but CUDA is not available, it says **"CPU-only wheel installed"** and
   prints the exact `uv sync` command to fix it. Guessing at this is how hours disappear.
3. The device selection in `vision/device.py` is `auto | cuda | cpu`, from config. `auto`
   logs what it chose and why, every start. A CPU fallback that nobody notices is a
   performance mystery waiting to happen.
4. No code anywhere calls `.cuda()` or `.to("cuda")` directly. One `Device` object is
   resolved at startup and passed down. Grep gate.

### VRAM budget (estimates — to be replaced by measurement in P1)

6144 MiB total, with Windows WDDM and the desktop compositor already holding some.

| Consumer | Estimate |
|----------|----------|
| Windows desktop / WDDM reserve | 400–900 MiB (varies with displays) |
| Torch CUDA context + cuDNN workspace | 500–800 MiB |
| YOLOv8n/s weights, FP16 | 15–60 MiB |
| Activations, batch 4 at 640x640, FP16 | 400–900 MiB |
| Allocator fragmentation headroom | 400 MiB |
| **Projected total** | **~1.8–3.1 GiB** |

That leaves room for one secondary model later. `vision.vram_budget_mb` (default 4000) is
a hard guard: loading a model whose projected footprint would exceed the remaining budget
**fails with `CapabilityError`** rather than letting CUDA OOM mid-stream. Modules depending
on that model are then reported unavailable (D-008). Projection starts as a declared
per-model estimate and is replaced by measured values from `vigil bench` as they are
obtained.

## 5. Console dependencies

| Package | Purpose | Notes |
|---------|---------|-------|
| `react`, `react-dom` 18 | UI runtime | |
| `typescript` 5.6 | Type safety | `strict: true`, `noUncheckedIndexedAccess: true` |
| `vite` 5 | Dev server and build | |
| `@tanstack/react-query` 5 | Server state: caching, refetch, invalidation | Removes the hand-rolled fetch/loading/error cycle that otherwise metastasises through every view |
| `zustand` 5 | Local UI state (layout, filters, selection) | Small; no provider tree |
| `react-router` 6 | View routing | |
| `zod` 3 | Runtime validation of API and WS payloads | TypeScript types vanish at runtime; a schema-mismatched WS frame must fail loudly, not corrupt a view |
| `openapi-typescript` (dev) | OpenAPI -> `types.gen.ts` | Makes the front/back contract machine-checked |
| `vitest`, `@testing-library/react` (dev) | Component tests | |

**Deliberately absent:** no component library (MUI, Chakra, shadcn), no Tailwind, no charting
library yet, no animation library, no icon mega-package.

- A component library imposes a visual identity, and a distinctive identity is a stated
  requirement. Styling is hand-written CSS with design tokens
  ([08-ui-architecture.md](08-ui-architecture.md) §6).
- Charts: the System and Analytics views need sparklines, time series, and a severity
  histogram. These are rendered as inline SVG from a small local `charts/` directory until
  there is a real need the hand-rolled version cannot meet. Revisit at P5; if a library
  becomes justified, it is one decision recorded here, not a default.
- Animation: CSS transitions only. Restrained animation is a requirement, and a
  spring-physics library is an invitation to violate it.
- Icons: a hand-curated inline SVG set. A monitoring console needs roughly fifteen glyphs.

## 6. Licensing — the Ultralytics question

**`ultralytics` is AGPL-3.0.** This is not a footnote; it has consequences.

- For local, private, non-distributed use (this project today), AGPL imposes no practical
  obligation.
- If VIGIL-88 is ever distributed, or offered to users over a network, AGPL-3.0 requires
  releasing the complete corresponding source of the whole work under AGPL — or buying an
  Ultralytics commercial licence.
- The AGPL network clause is triggered by exactly the thing this architecture does: serving
  a UI over HTTP.

**Mitigation, built in from the start.** Every detector sits behind the `Detector` protocol
with its own backend module ([02-directory-structure.md](02-directory-structure.md), `vision/backends/`).
`ultralytics` is used for development velocity in P1 and never imported outside
`vision/backends/ultralytics.py`. The `onnxruntime` backend (MIT) consuming exported ONNX
weights is the licence-clean path, and keeping it working is a P1 exit criterion — not a
future rescue project. Switching backends must be a config change.

Model **weights** carry their own terms, separate from the framework's. YOLOv8 weights are
AGPL; RT-DETR and several alternatives are Apache-2.0. `scripts/fetch_models.ps1` records
the source URL, licence, and sha256 of every weight file it downloads into
`var/models/MANIFEST.json`. No weights are committed to the repository.

## 7. Supply chain

| Control | Practice |
|---------|----------|
| Lockfile | `uv.lock` and `package-lock.json` committed; CI installs with `--frozen` |
| Hashes | `uv` records hashes in the lock; installs verify them |
| Weights | sha256-verified against `MANIFEST.json` on every load; a mismatch is a hard error, not a warning |
| Audit | `uv pip list --outdated` and `npm audit` reviewed at each phase boundary, not continuously |
| New dependency | Requires a row in this document stating purpose and the rejected alternative |
| Vendoring | Never. If a package is too risky to depend on, it is too risky to copy in |

## 8. Rejected dependencies

Recorded so the same ground is not re-litigated.

| Rejected | Instead | Reason |
|----------|---------|--------|
| `deepstream` / `gstreamer` | OpenCV + FFmpeg | Enormous Windows setup cost; the whole point is a system that installs on this desktop |
| `celery` / `redis` / `rabbitmq` | `queue.Queue`, `core/bus.py` | Single process (D-004). A broker adds an operational dependency and latency to solve a problem we do not have |
| `redis` as a cache | In-process dicts and ring buffers | Same |
| `pandas` | `sqlite3` aggregate SQL, `statistics` | A 50 MB dependency to compute counts that SQL already computes. Revisit only if Analytics outgrows SQL |
| `django` / `flask` | `fastapi` | OpenAPI generation is the deciding feature (§5) |
| `pytorch-lightning` | — | Training framework; we do not train |
| `supervision` / `boxmot` | Own ByteTrack in `tracking/` | Tracking is ~300 lines of code we must understand deeply to debug ID switches, which is the dominant tracking failure. Revisit if a stronger re-ID tracker is needed at P6 |
| `loguru` | `structlog` | Structured context binding is the requirement; loguru is string-formatting-first |
| `poetry` | `uv` | D-009 |
| `docker` for dev | Native Windows | GPU passthrough on Windows containers is friction with no payoff for a single-target desktop app |
