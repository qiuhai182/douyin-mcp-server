@echo off
REM ============================================================
REM  Quick upload: pull from remotes, convert encodings, commit,
REM  push to gitee + github. (ASCII only - cmd parses by codepage.)
REM  Usage: double-click or run "upLoad.bat" in any terminal.
REM ============================================================
setlocal
cd /d "%~dp0"

REM Prefer the project venv interpreter; fall back to PATH python.
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo [1/4] git pull gitee main ...
git pull gitee main
echo [2/4] git pull github main ...
git pull github main
echo [3/4] convert to UTF-8 ...
"%PY%" convert_to_utf8.py || goto :fail
echo [4/4] commit and push ...
git add --all -- ":!nul" || goto :fail
git commit -m "Quick upload: latest executables and code" || echo Nothing to commit.
echo pushing gitee ...
git push gitee main || goto :fail
echo pushing github ...
git push github main || goto :fail

echo.
echo ============ UPLOAD OK ============
exit /b 0

:fail
echo.
echo ============ UPLOAD FAILED ============
exit /b 1
