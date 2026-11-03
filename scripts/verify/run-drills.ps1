# Runs every automated failure drill and writes one evidence file saying what passed.
#
#   powershell -File scripts/verify/run-drills.ps1 [-Evidence out.json]
#
# Drills run here:
#   corrupted configuration, missing model, unreachable camera        (real app, real engine)
#   disk full, camera unplugged and recovered, gap in the footage    (real engine, simulated fault)
#
# "Simulated fault" is honest wording: the disk-full drill makes the evidence volume REPORT that it
# is nearly full, and the unplug drill makes a source fail its reads the way a pulled cable does.
# A real full volume and a real unplugged camera are drilled by hand (docs/verification/03).
param([string]$Evidence = "")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

function Run([string]$Name, [scriptblock]$Body) {
    $began = Get-Date
    $ErrorActionPreference = "Continue"  # a native tool's warning on stderr is not a failure; its exit code is
    & $Body *> $null
    [ordered]@{ drill = $Name; passed = ($LASTEXITCODE -eq 0); seconds = [math]::Round(((Get-Date) - $began).TotalSeconds, 1) }
}

$rows = @()
Push-Location desktop
try { $rows += Run "app: corrupted config, missing model, unreachable camera" { npm run e2e -- drills.spec.ts } } finally { Pop-Location }
$rows += Run "engine: disk full, unplug and recovery, gap in footage" { uv run pytest tests/integration/test_failure_drills.py -q }

$result = [ordered]@{
    drills       = $rows
    measured_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
    not_run      = @("a real full volume", "a real unplugged camera", "a GPU driver reset")
    verdict      = if (($rows | Where-Object { -not $_.passed }).Count -eq 0) { "All automated drills passed." } else { "FAILED: see drills." }
}
$json = $result | ConvertTo-Json -Depth 4
$json
if ($Evidence -ne "") { $json | Set-Content $Evidence -Encoding utf8 }
if (($rows | Where-Object { -not $_.passed }).Count -gt 0) { exit 1 }
