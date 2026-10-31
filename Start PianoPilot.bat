@echo off
title PianoPilot
cd /d "%~dp0"
rem Use the Python that comes with PianoPilot when it works, otherwise the one installed on this PC.
set "PY="
if exist "runtime\python.exe" (
  "runtime\python.exe" -E -s -c "import sys" >NUL 2>NUL
  if errorlevel 1 (
    echo PianoPilot's own Python ^(the runtime folder^) looks damaged. Download PianoPilot again to fix it.
    echo Trying the Python installed on this PC instead.
  ) else (
    set "PY=runtime\python.exe -E -s"
  )
)
if not defined PY (
  python -c "import sys" >NUL 2>NUL
  if errorlevel 1 (
    echo PianoPilot's own Python ^(the runtime folder^) is missing or damaged, and Python isn't installed on this PC.
    echo Download PianoPilot again ^(the download includes Python^), or install it from https://www.python.org/downloads/
    pause
    exit /b 1
  )
  set "PY=python"
)
start "PianoPilot" /min cmd /c "%PY% pianopilot.py || pause"
