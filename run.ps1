$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Python environment not found. Run .\build_exe.ps1 first."
}

$env:PYTHONPATH = Join-Path $projectRoot "src"
& $python -m drawing_assist.web_app @args
