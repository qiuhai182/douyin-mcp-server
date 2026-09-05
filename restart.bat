@echo off
rem Restart the douyin-mcp-server WebUI: stop old processes, then start again.
chcp 65001 >nul
title Douyin MCP Server - Restart

cd /d "%~dp0"

echo Stopping existing WebUI processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'web\\app\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Output ('  stopped ' + $_.ProcessId) } catch {} }"

rem give the port a moment to be released
rem (ping-based sleep: see note in start.bat about timeout + Ctrl+C)
ping -n 3 127.0.0.1 >nul

call "%~dp0start.bat"
