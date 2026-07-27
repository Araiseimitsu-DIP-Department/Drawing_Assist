$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $projectRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$runtimePython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (-not (Test-Path $python)) {
    if (Test-Path $runtimePython) {
        & $runtimePython -m venv $venv
    } else {
        py -3 -m venv $venv
    }
}

& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $projectRoot "requirements.txt")
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "DrawingDimensionHighlighter" `
    --paths (Join-Path $projectRoot "src") `
    --add-data "$projectRoot\src\drawing_assist\web;drawing_assist\web" `
    --collect-all webview `
    --exclude-module PyQt5 `
    --exclude-module PyQt6 `
    --exclude-module PySide2 `
    --exclude-module PySide6 `
    (Join-Path $projectRoot "src\drawing_assist\web_app.py")

Write-Host ""
Write-Host "Build complete: $projectRoot\dist\DrawingDimensionHighlighter.exe"
