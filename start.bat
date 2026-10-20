@echo off
rem ChatAgent one-click start (Windows): auto-restart on crash + file logging.
rem NOTE: keep this file pure ASCII. Non-ASCII text in a batch file combined
rem with `chcp` mid-file is known to desync cmd's parser (e.g. 'evel' is not
rem recognized), which makes the script randomly break.
title ChatAgent
cd /d %~dp0backend

rem Refuse to double-start: if port 8320 is already listening, exit with a hint.
netstat -ano | findstr :8320 | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
  echo [ChatAgent] Port 8320 is already in use. The service is probably running at http://127.0.0.1:8320
  echo [ChatAgent] To restart, please run stop.bat first, then this script again.
  pause
  exit /b 1
)

if not exist logs mkdir logs
if not exist .venv (
  echo [ChatAgent] First run: initializing environment via "uv sync" ...
  uv sync
  if errorlevel 1 (
    echo [ChatAgent] uv sync failed. Please make sure "uv" is installed and on PATH.
    pause
    exit /b 1
  )
)

set PYTHONIOENCODING=utf-8
echo ================================================
echo  ChatAgent starting...
echo    URL :  http://127.0.0.1:8320
echo    Log :  backend\logs\service.log
echo    Stop:  close this window, or run stop.bat
echo  (A silent console is normal. Details go to the log file above.)
echo ================================================
:loop
echo [%date% %time%] ChatAgent start >> logs\service.log
uv run uvicorn app.main:app --host 127.0.0.1 --port 8320 --no-access-log >> logs\service.log 2>&1
echo [%date% %time%] Server exited (code %errorlevel%). Restarting in 5s... see backend\logs\service.log
echo [%date% %time%] ChatAgent exit (code %errorlevel%), restart in 5s >> logs\service.log
timeout /t 5 /nobreak > nul
goto loop
