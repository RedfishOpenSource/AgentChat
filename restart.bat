@echo off
rem ChatAgent service menu launcher.
rem Keep this file pure ASCII (same reason as start.bat). Chinese UI lives in
rem scripts/restart.ps1 (UTF-8 with BOM), run via PowerShell.
chcp 65001 >nul
title ChatAgent Service Menu
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart.ps1"
if errorlevel 1 (
  echo.
  echo Script exited with an error. See messages above.
  pause
)
