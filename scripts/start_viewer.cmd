@echo off
setlocal
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] Virtual environment Python not found: %PY%
  echo Create it first: python -m venv .venv
  exit /b 1
)

pushd "%ROOT%"
"%PY%" -m uvicorn viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log %*
popd
