@echo off
REM Double-click to set up (first time) and start memebot in paper mode.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv || (echo Python 3.11+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || (echo Install failed & pause & exit /b 1)
)
if not exist .env copy .env.example .env >nul
.venv\Scripts\python -m memebot init --data data
echo.
echo Panel: http://127.0.0.1:8787   (Ctrl+C to stop)
.venv\Scripts\python -m memebot run --data data
pause
