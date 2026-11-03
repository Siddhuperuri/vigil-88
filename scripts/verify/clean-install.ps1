# Installs the built installer and checks, with nothing but Windows, that the application starts.
#
#   powershell -File scripts/verify/clean-install.ps1 -Installer <Setup exe> [-Evidence out.json]
#
# Meant for a CLEAN machine or virtual machine: one with no Python, no Node and no checkout, signed
# in as an ordinary user. It needs nothing from this repository at run time, so it can be copied to
# the machine with the installer. The evidence records which development tools it FOUND, so a pass
# on a machine that has them says so instead of claiming to be a clean-machine pass.
#
# What it checks: no administrator prompt was needed, the program and a Start-menu entry exist, an
# uninstall entry exists, the application starts the engine it ships with, the engine answers on
# loopback, the first-run state is reported, and quitting leaves no engine behind.
param([Parameter(Mandatory = $true)][string]$Installer, [string]$Evidence = "", [string]$InstallDir = "")
$ErrorActionPreference = "Stop"

$checks = [ordered]@{}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$env_ = [ordered]@{
    os                 = (Get-CimInstance Win32_OperatingSystem).Caption + " " + (Get-CimInstance Win32_OperatingSystem).BuildNumber
    elevated           = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    memory_gb          = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
    dev_tools_found    = @("python", "node", "uv", "git", "npm") | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue }
    nvidia_driver      = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
}
$clean = @($env_.dev_tools_found).Count -eq 0 -and -not $env_.elevated
if ($InstallDir -eq "") { $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\VIGIL-88" }

$engine = $null
try {
    $p = Start-Process -FilePath $Installer -ArgumentList "/S" -PassThru -Wait
    $checks["install_exit_code_zero"] = ($p.ExitCode -eq 0)
    $exe = Join-Path $InstallDir "VIGIL-88.exe"
    $checks["program_installed"] = Test-Path $exe
    $checks["engine_shipped_inside"] = Test-Path (Join-Path $InstallDir "resources\engine\vigil-engine.exe")
    $checks["console_shipped_inside"] = Test-Path (Join-Path $InstallDir "resources\ui\index.html")
    $start = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    $checks["start_menu_entry"] = [bool](Get-ChildItem $start -Recurse -Filter "VIGIL-88*.lnk" -ErrorAction SilentlyContinue)
    $checks["uninstall_entry"] = [bool](Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue |
        Where-Object { (Get-ItemProperty $_.PSPath).DisplayName -like "VIGIL-88*" })

    $app = Start-Process -FilePath $exe -PassThru
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and -not $engine) {
        Start-Sleep -Seconds 2
        $engine = Get-Process -Name "vigil-engine" -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    $checks["application_started_its_engine"] = [bool]$engine
    if ($engine) {
        Start-Sleep -Seconds 5
        # A frozen engine is a launcher process and the program it starts; either may own the socket.
        $ids = @(Get-Process -Name "vigil-engine" -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
        $listening = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $ids -contains $_.OwningProcess })
        $checks["engine_listens"] = $listening.Count -gt 0
        $checks["engine_listens_on_loopback_only"] = $listening.Count -gt 0 -and -not ($listening | Where-Object { $_.LocalAddress -notin @("127.0.0.1", "::1") })
        if ($listening.Count -gt 0) {
            $status = Invoke-RestMethod "http://127.0.0.1:$($listening[0].LocalPort)/api/v1/auth/status" -Headers @{ "X-Vigil-Request" = "1" }
            $checks["first_run_reported"] = [bool]$status.setup_required
        }
    }
} finally {
    Get-Process -Name "VIGIL-88" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    $checks["no_engine_left_after_quit"] = -not (Get-Process -Name "vigil-engine" -ErrorAction SilentlyContinue)
    Get-Process -Name "vigil-engine" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

$ok = -not ($checks.Values -contains $false)
$result = [ordered]@{
    machine        = $env_
    counts_as_a_clean_machine = $clean
    checks         = $checks
    installer      = Split-Path $Installer -Leaf
    measured_utc   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
    verdict        = if (-not $ok) { "FAILED: see checks." } elseif ($clean) { "Passed on a machine with no development tools." } else { "Passed, but this machine has development tools or ran elevated: NOT a clean-machine result." }
}
$json = $result | ConvertTo-Json -Depth 4
$json
if ($Evidence -ne "") { $json | Set-Content $Evidence -Encoding utf8 }
if (-not $ok) { exit 1 }
