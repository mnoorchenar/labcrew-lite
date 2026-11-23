@echo off
rem Start the app, then open http://localhost:8770 (API docs: http://localhost:8770/docs).
rem   run.bat        real LLM (Groq key from .env)
rem   run.bat mock   offline scripted agent: instant, no API calls
rem Runs in the current window: no new PowerShell or console is opened. Stop with Ctrl+C.
cd /d "%~dp0"
rem Python: this folder's own .venv if it exists, else the original labcrew one.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\labcrew\.venv\Scripts\python.exe"
set PYTHONIOENCODING=utf-8
if /i "%~1"=="mock" (set MOCK_LLM=1) else (set MOCK_LLM=0)
echo MOCK_LLM=%MOCK_LLM%  -  open http://localhost:8770
"%PY%" -m uvicorn gwascrew.api:app --port 8770
