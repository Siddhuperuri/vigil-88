# Builds the Windows installer: freeze the CPU engine, build the console, build the NSIS setup.
#
#   powershell -File scripts/package.ps1
#
# Output: build\installer\VIGIL-88-Setup-<version>.exe, UNSIGNED. Nothing here signs, uploads or
# publishes anything. It uses a throwaway virtual environment under build\ so the development
# environment (which may carry the GPU runtime) is not touched and not frozen into the engine.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Step([string]$Name, [scriptblock]$Body) {
    Write-Host "==> $Name"
    & $Body
    if ($LASTEXITCODE -ne 0) { throw "PACKAGE FAILED: $Name" }
}

Step "clean engine environment (CPU only)" {
    if (Test-Path build\engine-venv) { Remove-Item build\engine-venv -Recurse -Force }
    uv venv build\engine-venv --python 3.11
    $env:VIRTUAL_ENV = ""
    uv pip install --python build\engine-venv\Scripts\python.exe ".[capture,onnx-cpu,api,credentials,reports]" pyinstaller
}

Step "freeze the engine" {
    if (Test-Path build\dist) { Remove-Item build\dist -Recurse -Force }
    build\engine-venv\Scripts\pyinstaller.exe --noconfirm --clean --onedir --name vigil-engine `
        --distpath build\dist --workpath build\work --specpath build `
        --collect-submodules vigil --collect-submodules uvicorn --collect-submodules keyring.backends `
        --collect-data vigil --copy-metadata keyring --copy-metadata vigil-88 `
        --hidden-import argon2 --hidden-import cv2_enumerate_cameras `
        scripts\engine_entry.py
}

Step "the frozen engine starts and reports its checks" {
    build\dist\vigil-engine\vigil-engine.exe doctor
}

Step "third-party notices" {
    $python = (Resolve-Path build\engine-venv\Scripts\python.exe).Path
    uv run python scripts/notices.py $python | Out-File -Encoding utf8 docs\THIRD-PARTY-NOTICES.md
}

Step "installer" {
    Push-Location desktop
    try { npm run dist } finally { Pop-Location }
}

Write-Host "Built: $(Get-ChildItem build\installer\VIGIL-88-Setup-*.exe | Select-Object -First 1 -ExpandProperty FullName)"
Write-Host "UNSIGNED. Do not distribute: the project licence is not declared (plan decision 1)."
