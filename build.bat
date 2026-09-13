@echo off
REM ============================================================
REM  One-shot build: service exe (PyInstaller) + VS Code ext vsix
REM  Outputs:
REM    dist\douyin-server\douyin-server.exe   (tray service)
REM    vscode-extension\*.vsix                (VS Code/Trae extension)
REM
REM  NOTE 1: this script only BUILDS. It never starts / stops /
REM          restarts any service and never runs build artifacts.
REM  NOTE 2: keep this file ASCII-only! cmd parses .bat files in
REM          the OEM codepage (GBK on zh-CN Windows); UTF-8 Chinese
REM          comments turn into garbage commands.
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

where npx.cmd >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js/npx not found in PATH - needed for the vsix build
    exit /b 1
)

echo.
echo [1/2] Building service exe (PyInstaller)...
tasklist /FI "IMAGENAME eq douyin-server.exe" 2>nul | find /I "douyin-server.exe" >nul && (
    echo   [WARN] douyin-server.exe is running - it may lock dist files and fail the build.
    echo          Close it from the tray first if the build reports file-lock errors.
)
if not exist ".venv\Scripts\pyinstaller.exe" (
    echo   installing pyinstaller...
    ".venv\Scripts\python.exe" -m pip install --quiet pyinstaller || goto :fail
)
".venv\Scripts\pyinstaller.exe" build_exe.spec --noconfirm || goto :fail
echo   exe ready: dist\douyin-server\douyin-server.exe

echo.
echo [2/2] Building VS Code extension (vsce)...
pushd vscode-extension
call npx.cmd --yes @vscode/vsce package --no-dependencies || (popd & goto :fail)
popd

echo.
echo ================= BUILD OK =================
for %%f in ("vscode-extension\*.vsix") do echo   vsix: %%f
echo   exe : dist\douyin-server\douyin-server.exe
echo ============================================
exit /b 0

:fail
echo.
echo ================ BUILD FAILED ================
exit /b 1
