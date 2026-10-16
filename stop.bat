@echo off
rem ChatAgent 停止：按端口找到进程并结束
chcp 65001 >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
  echo 结束进程 %%p
  taskkill /F /PID %%p
)
echo ChatAgent 已停止。
