# What the antivirus on THIS computer makes of the installer and of the running application.
#
#   powershell -File scripts/verify/av-check.ps1 [-Evidence out.json]
#
# It uses Windows Security (Microsoft Defender) only. That is one engine on one machine: it says
# nothing about any other product, and a frozen Python program is a known source of false positives
# elsewhere. It also reports whether the folder it scanned was excluded from scanning, because a
# scan of an excluded folder proves nothing.
param([string]$Evidence = "", [int]$RunSeconds = 30, [string]$Root = "build\installer")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))

$results = [ordered]@{}
# A custom scan takes a FOLDER, so the folder holding the installer and the unpacked application is scanned.
if (-not (Test-Path $Root)) { throw "Nothing to scan: build the installer first (scripts\package.ps1)." }
$targets = @((Resolve-Path $Root).Path)
$setups = Get-ChildItem $Root -Filter "VIGIL-88-*Setup-*.exe" -ErrorAction SilentlyContinue

try { $status = Get-MpComputerStatus } catch { throw "Windows Security is not available here: $($_.Exception.Message)" }
$results["engine"] = "Microsoft Defender $($status.AMProductVersion)"
$results["real_time_protection"] = [bool]$status.RealTimeProtectionEnabled
$results["signatures"] = "$($status.AntivirusSignatureVersion) from $($status.AntivirusSignatureLastUpdated.ToUniversalTime().ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture))"

$excluded = @((Get-MpPreference).ExclusionPath) | Where-Object { $_ }
$results["scanned_folders_excluded"] = @($targets | Where-Object { $t = $_; $excluded | Where-Object { $t -like "$_*" } }).Count -gt 0

$since = Get-Date
foreach ($t in $targets) { Start-MpScan -ScanType CustomScan -ScanPath $t }
$results["scanned"] = @("$(Split-Path $targets[0] -Leaf)\ (the installer and the unpacked application)")
$found = @(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -ge $since })
$results["detections_during_scan"] = $found.Count

# Behaviour: run it for a little while with real-time protection watching.
$exe = Join-Path (Resolve-Path "$Root\win-unpacked").Path "VIGIL-88.exe"
$profile = Join-Path ([IO.Path]::GetTempPath()) ("vigil-av-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$ran = $false
if (Test-Path $exe) {
    $p = Start-Process -FilePath $exe -ArgumentList "--user-data-dir=$profile" -PassThru
    Start-Sleep -Seconds $RunSeconds
    $ran = -not $p.HasExited
    Get-CimInstance Win32_Process | Where-Object { $_.Name -like "vigil-engine*" -and $_.CommandLine -like "*$profile*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Remove-Item $profile -Recurse -Force -ErrorAction SilentlyContinue
}
$results["application_ran_for_seconds"] = if ($ran) { $RunSeconds } else { 0 }
$later = @(Get-MpThreatDetection | Where-Object { $_.InitialDetectionTime -ge $since })
$results["detections_total"] = $later.Count

foreach ($s in $setups) {
    $sig = Get-AuthenticodeSignature $s.FullName
    $results["installer_signature"] = "$($sig.Status)"
}
$results["smartscreen_note"] = "An unsigned installer is expected to trigger a SmartScreen warning when downloaded; this script cannot show that."
$results["measured_utc"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
$results["verdict"] =
    if ($results["scanned_folders_excluded"]) { "NOT VERIFIED: the scanned location is excluded from scanning." }
    elseif ($results["detections_total"] -eq 0) { "No detection by Microsoft Defender on this computer. Other products were not tried." }
    else { "DETECTED: $($results['detections_total']) item(s); see Get-MpThreatDetection." }
$json = $results | ConvertTo-Json
$json
if ($Evidence -ne "") { $json | Set-Content $Evidence -Encoding utf8 }
if ($results["detections_total"] -gt 0 -or $results["scanned_folders_excluded"]) { exit 1 }
