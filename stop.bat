@echo off
rem Stop the douyin-mcp-server WebUI.
chcp 65001 >nul
title Douyin MCP Server - Stop

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'web\\app\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Output ('  stopped ' + $_.ProcessId) } catch {} }"

echo [OK] Stopped (if any were running).
ping -n 2 127.0.0.1 >nul
