# The full quality gate (docs/architecture/01 section 12). A change is not done until this passes.
#   powershell -File scripts/check.ps1            # everything
#   powershell -File scripts/check.ps1 -SkipRun   # static checks and tests only
param([switch]$SkipRun)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Step([string]$Name, [scriptblock]$Command) {
    Write-Host ""
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "GATE FAILED: $Name" }
}

# Coverage floors apply to the logic that decides things, not to I/O glue (09 section 3).
$Floors = @(
    @{ Path = "src/vigil/domain/*";        Min = 90 },
    @{ Path = "src/vigil/core/*";          Min = 90 },
    @{ Path = "src/vigil/config/*";        Min = 85 },
    @{ Path = "src/vigil/observability/*"; Min = 85 },
    @{ Path = "src/vigil/vision/*";        Min = 90 },
    @{ Path = "src/vigil/pipeline/*";      Min = 90 },
    @{ Path = "src/vigil/api/*";           Min = 90 },
    @{ Path = "src/vigil/bench/*";         Min = 88 },
    @{ Path = "src/vigil/tracking/*";      Min = 95 },
    @{ Path = "src/vigil/scene/*";         Min = 90 },
    # Storage and response are I/O-heavy, so the floors are lower than the decision engines'
    # -- but the parts that must not be wrong (the query builders, the lifecycle machine, the
    # evidence failure paths) are covered well above these numbers.
    @{ Path = "src/vigil/persistence/*";   Min = 85 },
    @{ Path = "src/vigil/response/*";      Min = 90 },
    # The logic that decides whether to alarm. Highest floors in the project (09 section 3):
    # a bug in temporal/ makes every event's confidence wrong, and one in verification/
    # decides what an operator never sees.
    @{ Path = "src/vigil/temporal/*";      Min = 95 },
    @{ Path = "src/vigil/verification/*";  Min = 95 },
    @{ Path = "src/vigil/severity/*";      Min = 90 },
    @{ Path = "src/vigil/events/*";        Min = 90 },
    # Analytics reports numbers an operator will act on, and a wrong one is indistinguishable
    # from a right one by looking at it -- so the arithmetic, the empty-window paths and the
    # CSV neutralisation are all covered.
    @{ Path = "src/vigil/analytics/*";     Min = 95 }
)

Step "ruff format --check" { uv run ruff format --check . }
Step "ruff check"          { uv run ruff check . }
Step "mypy --strict"       { uv run mypy }
Step "import-linter (layers, purity)" { uv run lint-imports }
Step "repository gates (clock, except, paths, secrets, size)" { uv run python scripts/gates.py }
Step "pytest"              { uv run pytest --cov=vigil --cov-report= }
foreach ($f in $Floors) {
    Step "coverage >= $($f.Min)% for $($f.Path)" {
        uv run coverage report --include=$($f.Path) --fail-under=$($f.Min) --skip-covered
    }
}
Step "generated config reference is current" {
    uv run vigil doctor --dump-config docs/CONFIG.md | Out-Null
    git diff --exit-code -- docs/CONFIG.md
}
Step "performance report is current" {
    uv run vigil bench report | Out-Null
    git diff --exit-code -- docs/PERFORMANCE.md
}
# ---------------------------------------------------------------- the console and the shell (D0)
# The same bar as the engine: strict types, lint with accessibility rules, formatting, tests with a
# coverage floor, and a script that reads the design tokens for contrast. Nothing here may be
# loosened to make a phase pass.
Push-Location ui
try {
    Step "ui: npm ci"                       { npm ci --no-audit --no-fund }
    Step "ui: typecheck (strict)"           { npm run typecheck }
    Step "ui: eslint (jsx-a11y, strict)"    { npm run lint }
    Step "ui: prettier --check"             { npm run format:check }
    Step "ui: contrast >= 4.5:1 over tokens" { npm run contrast }
    Step "ui: unit, component and axe tests with coverage floor" { npm test }
    Step "ui: production build"             { npm run build }
} finally { Pop-Location }
Step "generated API types are current" {
    uv run vigil openapi --out ui/src/api/openapi.json
    Push-Location ui
    try { npm run gen:types | Out-Null } finally { Pop-Location }
    git diff --exit-code -- ui/src/api/openapi.json ui/src/api/types.gen.ts
}
Push-Location desktop
try {
    Step "desktop: npm ci"                  { npm ci --no-audit --no-fund }
    Step "desktop: typecheck (strict)"      { npm run typecheck }
    Step "desktop: eslint (strict)"         { npm run lint }
    Step "desktop: prettier --check"        { npm run format:check }
    Step "desktop: unit tests with coverage floor" { npm test }
    Step "desktop: build"                   { npm run build }
} finally { Pop-Location }

if (-not $SkipRun) {
    Step "vigil doctor"        { uv run vigil doctor }
    Step "vigil run --selftest" { uv run vigil run --selftest }
    # Launches the real Electron app, which launches the real engine. Needs a desktop session.
    Push-Location desktop
    try { Step "desktop: end-to-end (real app, real engine, axe)" { npm run e2e } } finally { Pop-Location }
}

Write-Host ""
Write-Host "ALL GATES PASSED" -ForegroundColor Green
