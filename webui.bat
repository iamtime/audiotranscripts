@echo off
rem Launch the ushi web UI (http://127.0.0.1:8765) on Windows.
rem Uses .venv (setup_windows.ps1 creates it), or %VOICE_REC_PYTHON%, or python on PATH.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if defined VOICE_REC_PYTHON (
  set "PY=%VOICE_REC_PYTHON%"
) else if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
"%PY%" server.py %*
