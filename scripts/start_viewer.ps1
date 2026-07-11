$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "Virtual environment Python not found: $python`nCreate it first: python -m venv .venv"
}

Push-Location $root
try {
    & $python -m uvicorn viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log @args
} finally {
    Pop-Location
}
