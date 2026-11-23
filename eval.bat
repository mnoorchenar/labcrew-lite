@echo off
rem Run the evaluation harness. Results: eval\results\live.md (or mock.md). Runs in the current window.
rem   eval.bat        real LLM; answers are cached, so a re-run only calls Groq for new questions
rem   eval.bat mock   offline scripted agent (baseline)
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\labcrew\.venv\Scripts\python.exe"
set PYTHONIOENCODING=utf-8
if /i "%~1"=="mock" (set MOCK_LLM=1) else (set MOCK_LLM=0)
"%PY%" -m eval.run_eval
