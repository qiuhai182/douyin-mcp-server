@echo off
rem 一键全 UP 刷新（静默版）：以隐藏窗口运行 schtasks 守护刷新，全程无窗口。
rem 也可直接在 WebUI（http://localhost:8080）点「一键刷新全部」后台执行。
rem
rem - WebUI/托盘保持运行，driver 与 WebUI 通过心跳文件互斥，前端能看到
rem   实时进度、可以随时终止。
rem - 日志：output\刷新日志\driver_live.log
rem - 汇总：output\刷新日志\YYYYMMDD_HHMMSS_全UP刷新_汇总.log
rem
rem 可选参数：
rem   run_all_refresh.bat             默认 workers=3, force=false
rem   run_all_refresh.bat 5           workers=5
rem   run_all_refresh.bat 3 force     强制重跑所有已完成的视频
cd /d "%~dp0"

start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ^
    -File "%~dp0scripts\refresh_guarded.ps1" %*

exit /b 0
