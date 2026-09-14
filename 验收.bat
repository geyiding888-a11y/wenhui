@echo off
REM ===================================================================
REM  Wenhui - self check. Costs no API money.
REM
REM  !! KEEP THIS FILE PURE ASCII !!
REM  See the long note at the top of the main launcher .bat in this
REM  folder for why.
REM ===================================================================

chcp 65001 >nul
cd /d "%~dp0"

if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if errorlevel 1 (
    type "config\no-uv.txt"
    echo.
    pause
    exit /b 1
)

REM --extra dev because pytest lives in the dev extra and `uv sync`
REM alone does not install optional dependency groups.
uv sync --quiet --extra dev
if errorlevel 1 (
    type "config\sync-failed.txt"
    echo.
    pause
    exit /b 1
)

uv run --quiet python -X utf8 -m wenhui.launcher selfcheck

echo.
pause
