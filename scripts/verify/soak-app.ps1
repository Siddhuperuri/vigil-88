# Soaks the WHOLE application: the Electron shell, the console and the engine, driven through every
# view once a minute while every process is measured.
#
#   powershell -File scripts/verify/soak-app.ps1 -Minutes 480
#   powershell -File scripts/verify/soak-app.ps1 -Minutes 5 -Packaged "<path to an installed VIGIL-88.exe>"
#
# The plan asks for 480 minutes. Whatever -Minutes says is what the report says it ran for.
# Output goes to build\soak\<stamp>\ : samples.csv, summary.json, journey.json, report.md.
param(
    [double]$Minutes = 5,
    [string]$Packaged = ""
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ", [Globalization.CultureInfo]::InvariantCulture)
$dir = Join-Path "build\soak" $stamp
New-Item -ItemType Directory -Force $dir | Out-Null
$abs = (Resolve-Path $dir).Path

$env:VIGIL_SOAK_MINUTES = [string]$Minutes
$env:VIGIL_SOAK_DIR = $abs
if ($Packaged -ne "") { $env:VIGIL_E2E_PACKAGED = (Resolve-Path $Packaged).Path }

$python = (Get-Command uv).Source
$sampler = Start-Process -FilePath $python -PassThru -NoNewWindow -ArgumentList @(
    "run", "python", "scripts/verify/proc_sampler.py",
    (Join-Path $abs "app.pid"), [string]$Minutes, (Join-Path $abs "samples.csv"),
    "--json", (Join-Path $abs "summary.json")
) -RedirectStandardOutput (Join-Path $abs "sampler.txt")

Push-Location desktop
try { npm run e2e -- soak.spec.ts; $journey = $LASTEXITCODE } finally { Pop-Location }
$sampler.WaitForExit()

$summary = Get-Content (Join-Path $abs "summary.json") -Raw | ConvertFrom-Json
$report = @(
    "# Application soak",
    "",
    "- Started: $stamp (UTC)",
    "- Duration asked for: $Minutes minutes (the plan asks for 480)",
    "- Target: $(if ($Packaged -ne '') { 'installed application' } else { 'source tree (Electron + engine)' })",
    "- User journey (every view once a minute): $(if ($journey -eq 0) { 'PASSED' } else { 'FAILED, see journey.json' })",
    "- Process tree: $(if ($summary.bounded) { 'bounded on this run' } else { 'NOT shown to be bounded' })",
    "",
    "## Figures",
    ""
) + ($summary.lines | ForEach-Object { "- $_" })
$report | Set-Content (Join-Path $abs "report.md") -Encoding utf8
$report
if ($journey -ne 0 -or -not $summary.bounded) { exit 1 }
