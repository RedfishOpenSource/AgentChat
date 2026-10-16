@echo off
rem ChatAgent 一键启动（Windows）：崩溃自动重启 + 日志落盘
chcp 65001 >nul 2>&1
cd /d %~dp0backend

if not exist logs mkdir logs
if not exist .venv (
  echo [ChatAgent] 首次运行，初始化环境（需要几分钟）...
  uv sync || (echo 依赖安装失败，请确认已安装 uv & pause & exit /b 1)
)

set PYTHONIOENCODING=utf-8
:loop
echo [%date% %time%] ChatAgent 启动 >> logs\service.log
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log >> logs\service.log 2>&1
echo [%date% %time%] 服务退出（退出码 %errorlevel%），5 秒后自动重启 >> logs\service.log
timeout /t 5 /nobreak > nul
goto loop
