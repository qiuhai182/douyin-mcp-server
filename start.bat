@echo off
rem Start the douyin-mcp-server WebUI (http://localhost:8080)
chcp 65001 >nul
title Douyin MCP Server - WebUI

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run setup first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -e ".[web]"
    pause
    exit /b 1
)

echo Starting WebUI at http://localhost:8080 ...
start "douyin-webui" /min cmd /c ".venv\Scripts\python.exe web\app.py"

rem Wait until the server responds (max ~30s). Cold starts on Python 3.14
rem (bytecode compilation, antivirus scanning) can take well over 15s.
rem ping -n is used instead of timeout: timeout aborts the whole batch
rem when Ctrl+C is pressed somewhere ("Terminate batch job (Y/N)?").
set /a tries=0
:wait
ping -n 2 127.0.0.1 >nul
set /a tries+=1
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://localhost:8080/' -UseBasicParsing -TimeoutSec 2).StatusCode } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    if %tries% lss 30 goto wait
    echo [ERROR] Server did not respond within ~30s. It may still be
    echo starting in the background - try opening http://localhost:8080
    echo in a moment, or run manually to see the error:
    echo     .venv\Scripts\python.exe web\app.py
    pause
    exit /b 1
)

echo [OK] WebUI is running: http://localhost:8080
start http://localhost:8080
exit /b 0
