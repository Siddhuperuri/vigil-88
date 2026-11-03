# Exercises BOTH branches of the uninstaller on this computer: it keeps the person's data, and it
# removes it when told to. It installs the development installer silently into a throwaway folder,
# so it never touches an existing installation.
#
#   powershell -File scripts/verify/uninstall-branches.ps1
#
# It plants a marker file in the per-user data folder, so it REFUSES to run if that folder already
# holds anything: the "remove" branch deletes it, and a script must not be able to delete someone's
# evidence. Run it on a machine, or an account, that has never used the application.
param(
    [string]$Installer = "",
    [string]$Evidence = ""
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

if ($Installer -eq "") {
    $found = Get-ChildItem build\installer -Filter "VIGIL-88-*Setup-*.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $found) { throw "No installer in build\installer. Run scripts\package.ps1 first." }
    $Installer = $found.FullName
}
$data = Join-Path $env:APPDATA "VIGIL-88"
if ((Test-Path $data) -and (Get-ChildItem $data -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
    throw "REFUSING: $data already holds data. This script deletes that folder in one of its tests."
}
$dir = Join-Path ([IO.Path]::GetTempPath()) ("vigil-uninstall-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$results = [ordered]@{}

function Install-Silently {
    # /D= must be the last argument and unquoted: an NSIS rule.
    $p = Start-Process -FilePath $Installer -ArgumentList "/S", "/D=$dir" -PassThru -Wait
    if ($p.ExitCode -ne 0) { throw "Silent install failed: $($p.ExitCode)" }
    if (-not (Test-Path (Join-Path $dir "VIGIL-88.exe"))) { throw "Install left no VIGIL-88.exe in $dir" }
}
function Uninstall-With([string[]]$Extra) {
    $u = Get-ChildItem $dir -Filter "Uninstall*.exe" | Select-Object -First 1
    if ($null -eq $u) { throw "No uninstaller in $dir" }
    # _?= keeps the uninstaller in place, so Start-Process -Wait really waits for it.
    $p = Start-Process -FilePath $u.FullName -ArgumentList (@("/S") + $Extra + @("_?=$dir")) -PassThru -Wait
    return $p.ExitCode
}
function Plant { New-Item -ItemType Directory -Force $data | Out-Null; "marker" | Set-Content (Join-Path $data "marker.txt") }

try {
    # 1. A silent uninstall keeps the data.
    Install-Silently
    Plant
    $code = Uninstall-With @()
    $results["keep_exit_code"] = $code
    $results["keep_program_removed"] = -not (Test-Path (Join-Path $dir "VIGIL-88.exe"))
    $results["keep_data_preserved"] = Test-Path (Join-Path $data "marker.txt")
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue

    # 2. Told to, it removes the data.
    Install-Silently
    Plant
    $code = Uninstall-With @("/REMOVEDATA")
    $results["remove_exit_code"] = $code
    $results["remove_program_removed"] = -not (Test-Path (Join-Path $dir "VIGIL-88.exe"))
    $results["remove_data_removed"] = -not (Test-Path $data)
} finally {
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue
}

$ok = $results["keep_program_removed"] -and $results["keep_data_preserved"] -and
      $results["remove_program_removed"] -and $results["remove_data_removed"]
$results["installer"] = Split-Path $Installer -Leaf
$results["measured_utc"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$results["verdict"] = if ($ok) { "Both branches behaved: /S kept the data, /REMOVEDATA removed it." } else { "FAILED: see the fields above." }
$json = $results | ConvertTo-Json
$json
if ($Evidence -ne "") { $json | Set-Content $Evidence -Encoding utf8 }
if (-not $ok) { exit 1 }
