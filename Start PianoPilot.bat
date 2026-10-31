@echo off
title PianoPilot
cd /d "%~dp0"
rem Use the Python that comes with PianoPilot when it's there, otherwise the one installed on this PC.
set "PY=python"
if exist "runtime\python.exe" set "PY=runtime\python.exe"
%PY% -c "import sys" >NUL 2>NUL
if errorlevel 1 (
  echo PianoPilot's own Python ^(the runtime folder^) is missing, and Python isn't installed on this PC.
  echo Download PianoPilot again ^(the download includes Python^), or install it from https://www.python.org/downloads/
  pause
  exit /b 1
)
start "PianoPilot" /min cmd /c "%PY% pianopilot.py || pause"
