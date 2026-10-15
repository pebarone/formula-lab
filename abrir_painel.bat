@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ambiente .venv nao encontrado. Consulte o README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m dashboard.server --open
if errorlevel 1 pause
