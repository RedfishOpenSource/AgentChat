@echo off
rem ChatAgent stop: kill the process listening on port 8000.
rem NOTE: keep this file pure ASCII (see start.bat for the reason).
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
  echo Killing PID %%p
  taskkill /F /PID %%p
  set FOUND=1
)
if %FOUND%==0 echo No ChatAgent process found on port 8000.
echo Done.
