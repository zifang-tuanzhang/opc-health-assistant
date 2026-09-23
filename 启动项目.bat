@echo off
rem ---- OPC project launcher (keep this file pure ASCII) ----
rem Double-click to run: bootstrap venv, install deps offline, start backend, open browser.
setlocal EnableExtensions
set "HERE=%~dp0"
set "PYSCRIPT=%HERE%launcher.py"

rem -- resolve a Python 3.10+ interpreter, with three fallbacks --
set "PYCMD="
py -3 -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"

if not defined PYCMD (
  python -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1
  if not errorlevel 1 set "PYCMD=python"
)

if not defined PYCMD (
  echo.
  echo [OPC] Python 3.10+ not found on PATH.
  echo [OPC] Please install Python from https://www.python.org/downloads/
  echo [OPC] and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

%PYCMD% "%PYSCRIPT%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [OPC] Launcher exited with an error. See messages above.
  pause
  exit /b %RC%
)
endlocal
