@echo off
title PianoPilot
cd /d "%~dp0"
python -c "import sys" >NUL 2>NUL
if errorlevel 1 (
  echo Python isn't installed, or Windows is pointing at its Store shortcut instead.
  echo Install Python from https://www.python.org/downloads/ and try again.
  pause
  exit /b 1
)
start "PianoPilot" /min cmd /c "python pianopilot.py || pause"
