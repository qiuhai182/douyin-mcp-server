@echo off
rem Start the douyin-mcp-server WebUI as a system-tray app (no console).
rem The server runs hidden; right-click the tray icon for the menu
rem (open console / open log / quit). Double-click opens http://localhost:8080
chcp 65001 >nul
title Douyin MCP Server - WebUI (tray)

cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] .venv not found. Run setup first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -e ".[web]"
    pause
    exit /b 1
)

rem curl probe bypassing any system proxy (ProxyOverride does not always
rem apply to PowerShell); used for all reachability checks below.
set PROBE=curl.exe -s -o NUL -w "%%{http_code}" --noproxy * --max-time 3 http://127.0.0.1:8080/

rem Already running? Just open the console and leave the tray icon alone.
for /f %%c in ('%PROBE%') do set CODE=%%c
if "%CODE%"=="200" (
    echo [OK] WebUI is already running in the tray - opening console...
    start "" http://localhost:8080
    exit /b 0
)

echo Starting WebUI in system tray (hidden)...
start "" ".venv\Scripts\pythonw.exe" "tray_server.py"

rem Wait until the server responds (max ~30s). Cold starts on Python 3.14
rem (bytecode compilation, antivirus scanning) can take well over 15s.
rem ping -n is used instead of timeout: timeout aborts the whole batch
rem when Ctrl+C is pressed somewhere ("Terminate batch job (Y/N)?").
set /a tries=0
:wait
ping -n 2 127.0.0.1 >nul
set /a tries+=1
set CODE=
for /f %%c in ('%PROBE%') do set CODE=%%c
if not "%CODE%"=="200" (
    if %tries% lss 30 goto wait
    echo [ERROR] Server did not respond within ~30s. Check the runtime log:
    echo     logs\webui.log
    echo Or run in the foreground to see the error:
    echo     .venv\Scripts\python.exe web\app.py
    pause
    exit /b 1
)

echo [OK] WebUI is running in the tray: http://localhost:8080
start "" http://localhost:8080
exit /b 0
