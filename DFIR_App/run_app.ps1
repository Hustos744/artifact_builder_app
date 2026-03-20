$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$settingsPath = Join-Path $repoRoot "settings.json"
$workspaceDir = Join-Path $repoRoot "workspace"

if (-not (Test-Path $venvPython)) {
    throw "Virtual environment not found. Run .\bootstrap.ps1 first."
}

$env:DFIR_SETTINGS_FILE = $settingsPath
$env:DFIR_OUTPUT_DIR = $workspaceDir

Set-Location $repoRoot
& $venvPython -m streamlit run app.py
