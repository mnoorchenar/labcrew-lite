@echo off
rem Run all offline tests (no API calls, about 1.5 minutes). Runs in the current window.
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\labcrew\.venv\Scripts\python.exe"
set PYTHONIOENCODING=utf-8
"%PY%" -m pytest
