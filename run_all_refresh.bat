@echo off
rem 一键全 UP 刷新：串行遍历 authors.json 里所有 UP 的主页，
rem 增量刷新（已完整解析的视频自动跳过），workers=3 并发下载/解析。
rem
rem 运行前自动停托盘（driver 独占 Chrome profile），跑完自动重启托盘。
rem
rem 日志输出到 output\刷新日志\driver_*.log；跑完后会在同目录写一个
rem YYYYMMDD_HHMMSS_全UP刷新_汇总.log（JSON 格式，每个 UP 的 ok/skip/fail 统计）。
rem
rem 可选参数：
rem   run_all_refresh.bat             默认 workers=3, force=false
rem   run_all_refresh.bat 5           workers=5
rem   run_all_refresh.bat 3 force     强制重跑所有已完成的视频
chcp 65001 >nul
title 全 UP 刷新 driver

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found.
    pause
    exit /b 1
)

set "WORKERS=%~1"
if "%WORKERS%"=="" set "WORKERS=3"
set "FORCE=%~2"

echo === 全 UP 刷新 driver ===
echo workers=%WORKERS%  force=%FORCE%
echo 增量刷新 = force=false 时已完整解析的视频自动跳过
echo.

echo --- 停托盘（driver 独占 Chrome profile） ---
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'tray_server\.py|web\\app\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Output ('  stopped pid=' + $_.ProcessId) } catch {} }"
ping -n 3 127.0.0.1 >nul

echo --- 启动 driver ---
.venv\Scripts\python.exe scripts\refresh_all.py %WORKERS% %FORCE%

echo.
echo --- driver 退出，退出码=%ERRORLEVEL% ---

echo --- 重启托盘 ---
call start.bat

pause
