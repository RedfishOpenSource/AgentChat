# ChatAgent 服务管理菜单
# 选项 1：停止服务（含 start.bat 自动重启循环）
# 选项 2：重新编译前端并启动服务（后端托管页面），就绪后自行用浏览器打开测试
# 注意：本文件需保存为 UTF-8 with BOM，否则 Windows PowerShell 5.1 会乱码。

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot   # 项目根目录（scripts/ 的上一级）
$Port = 8320
$Url = "http://127.0.0.1:$Port"

function Get-ListenerPids([int]$TargetPort) {
    # 返回监听指定端口的进程 PID 列表（优先 Get-NetTCPConnection，失败回退 netstat）
    try {
        $conns = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction Stop
        return @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        $lines = netstat -ano | Select-String ":$TargetPort\s+.+LISTENING"
        return @($lines | ForEach-Object { ($_ -split '\s+')[-1].Trim() } | Select-Object -Unique)
    }
}

function Stop-ChatAgent {
    Write-Host "`n正在停止 ChatAgent ..." -ForegroundColor Cyan

    # 1) 结束 start.bat 的自动重启循环（cmd 窗口进程树，防止 5 秒后又被拉起）
    # 注意：必须排除 restart.bat 自身——"restart.bat" 包含子串 "start.bat"，
    # 否则会把自己所在的进程树杀掉，脚本直接中断。
    $loops = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
        Where-Object { $_.CommandLine -match 'start\.bat' -and $_.CommandLine -notmatch 'restart\.bat' }
    foreach ($p in $loops) {
        Write-Host "  结束重启循环进程树 PID $($p.ProcessId)"
        & taskkill /F /T /PID $p.ProcessId 2>$null | Out-Null
    }

    # 2) 结束仍在监听端口的进程树
    foreach ($procId in (Get-ListenerPids $Port)) {
        Write-Host "  结束服务进程树 PID $procId"
        & taskkill /F /T /PID $procId 2>$null | Out-Null
    }

    # 3) 兜底：清理残留的本项目 uvicorn 进程（命令行含 uvicorn 且路径含 ChatAgent）
    $strays = Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match 'ChatAgent' }
    foreach ($p in $strays) {
        Write-Host "  清理残留进程 PID $($p.ProcessId)"
        & taskkill /F /T /PID $p.ProcessId 2>$null | Out-Null
    }

    Start-Sleep -Seconds 1
    if (Get-ListenerPids $Port) {
        Write-Host "端口 $Port 仍被占用，可能有非本项目进程，请手动检查（netstat -ano | findstr :$Port）。" -ForegroundColor Red
    } else {
        Write-Host "服务已停止。" -ForegroundColor Green
    }
}

function Start-ChatAgent {
    Write-Host "`n[1/3] 编译检查后端代码 ..." -ForegroundColor Cyan
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Push-Location (Join-Path $Root 'backend')
        uv run python -m compileall -q app
        if ($LASTEXITCODE -ne 0) {
            Pop-Location
            Write-Host "后端代码编译检查失败，已取消启动。" -ForegroundColor Red
            return
        }
        Pop-Location
        Write-Host "  后端 OK"
    } else {
        Write-Host "  未找到 uv，跳过后端检查（start.bat 启动时仍需要 uv）" -ForegroundColor Yellow
    }

    Write-Host "[2/3] 重新编译前端 ..." -ForegroundColor Cyan
    Push-Location (Join-Path $Root 'frontend')
    npm run build
    $buildOk = $LASTEXITCODE -eq 0
    Pop-Location
    if (-not $buildOk) {
        Write-Host "前端编译失败，已取消启动。" -ForegroundColor Red
        return
    }

    Write-Host "[3/3] 启动服务 ..." -ForegroundColor Cyan
    Start-Process -FilePath (Join-Path $Root 'start.bat') -WorkingDirectory $Root -WindowStyle Hidden

    Write-Host "等待服务就绪（最长 30 秒）..."
    $ready = $false
    for ($i = 0; $i -lt 38; $i++) {
        Start-Sleep -Milliseconds 800
        try {
            $resp = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/health" -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
    }

    if ($ready) {
        Write-Host ""
        Write-Host "服务已启动: $Url" -ForegroundColor Green
        Write-Host "请用浏览器打开上面地址进行测试（建议 Ctrl+F5 强制刷新）。"
        Write-Host "运行日志: backend\logs\service.log；停止服务请回到菜单选 1。"
    } else {
        Write-Host "服务未在预期时间内就绪，请查看 backend\logs\service.log" -ForegroundColor Red
    }
}

while ($true) {
    Write-Host ""
    Write-Host "================ ChatAgent 服务管理 ================" -ForegroundColor White
    Write-Host "  1. 停止服务"
    Write-Host "  2. 重新编译并启动服务（然后自行用浏览器打开 $Url 测试）"
    Write-Host "  0. 退出"
    $choice = Read-Host "请选择"
    switch ($choice.Trim()) {
        '1' { Stop-ChatAgent }
        '2' { Start-ChatAgent }
        '0' { exit }
        default { Write-Host "无效选择，请输入 1 / 2 / 0" -ForegroundColor Yellow }
    }
}
