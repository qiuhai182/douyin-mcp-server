@echo off
rem Reload the douyin-mcp-server WebUI with the latest code.
rem
rem Preferred path: ask the running service to reload itself in place (same
rem PID, no console window, no interruption). If a batch job is in progress
rem the service answers 202 and queues the reload until the job finishes, so
rem running tasks are never cut off.
rem
rem Fallback: when no service is listening, just start one.
chcp 65001 >nul
title Douyin MCP Server - Restart
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "URL=http://127.0.0.1:8080"

echo Requesting silent reload at %URL% ...
for /f %%s in ('curl.exe --noproxy * --max-time 5 -s -o nul -w "%%{http_code}" -X POST "%URL%/api/service/restart"') do set "CODE=%%s"

if "%CODE%"=="200" (
    echo Reloading now.
    goto :wait
)
if "%CODE%"=="202" (
    echo A batch job is running - the reload is queued and will run when it finishes.
    goto :done
)

echo No running service answered (code=%CODE%), starting a new one...
call "%~dp0start.bat"
goto :done

:wait
rem Wait for the reloaded service to answer again (it keeps the same PID).
for /l %%i in (1,1,60) do (
    for /f %%s in ('curl.exe --noproxy * --max-time 2 -s -o nul -w "%%{http_code}" "%URL%/api/health"') do set "CODE=%%s"
    if "!CODE!"=="200" (
        echo Reloaded successfully.
        goto :done
    )
    ping -n 2 127.0.0.1 >nul
)
echo WARNING: service did not come back within about 60 seconds.

:done
ping -n 3 127.0.0.1 >nul
