$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    throw "Virtual environment not found at $VenvPy. Run scripts\setup-dev.ps1 first."
}
& $VenvPy -m ropi @args
