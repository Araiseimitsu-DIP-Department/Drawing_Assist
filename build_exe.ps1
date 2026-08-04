$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $projectRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$runtimePython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$appName = [string]([char]0x52A0) + [char]0x5DE5 + [char]0x56F3 + [char]0x9762 + [char]0x4F5C + [char]0x6210 + [char]0x652F + [char]0x63F4 + [char]0x30C4 + [char]0x30FC + [char]0x30EB
$iconPath = Join-Path $projectRoot "assets\app_icon.ico"
$specOutputPath = Join-Path $projectRoot "tmp"

if (-not (Test-Path $python)) {
    if (Test-Path $runtimePython) {
        & $runtimePython -m venv $venv
    } else {
        py -3 -m venv $venv
    }
}

& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $projectRoot "requirements.txt")
if (-not (Test-Path $iconPath)) {
    throw "アイコンファイルが見つかりません: $iconPath"
}
New-Item -ItemType Directory -Path $specOutputPath -Force | Out-Null
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $appName `
    --icon $iconPath `
    --specpath $specOutputPath `
    --paths (Join-Path $projectRoot "src") `
    --add-data "$projectRoot\src\drawing_assist\web;drawing_assist\web" `
    --add-data "$projectRoot\src\drawing_assist\windows_ocr.ps1;drawing_assist" `
    --collect-all webview `
    --exclude-module PyQt5 `
    --exclude-module PyQt6 `
    --exclude-module PySide2 `
    --exclude-module PySide6 `
    (Join-Path $projectRoot "src\drawing_assist\web_app.py")

Write-Host ""
Write-Host "Build complete: $projectRoot\dist\$appName.exe"
