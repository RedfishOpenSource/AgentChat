@echo off
rem ChatAgent stop: kill the supervisor (start.bat auto-restart loop) first, then the server on port 8320.
rem NOTE: keep this file pure ASCII (see start.bat for the reason).
set FOUND=0

rem 1) Stop the start.bat supervisor, otherwise it respawns the server after 5s.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'start\.bat' -and $_.Name -eq 'cmd.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

rem 2) Kill whatever is still listening on port 8320.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8320 ^| findstr LISTENING') do (
  echo Killing PID %%p
  taskkill /F /T /PID %%p
  set FOUND=1
)

if %FOUND%==0 echo No ChatAgent process found on port 8320.
echo Done.
