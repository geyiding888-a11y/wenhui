@echo off
REM ===================================================================
REM  Wenhui - set the AI API key.
REM
REM  !! KEEP THIS FILE PURE ASCII !!
REM  See the long note at the top of the main launcher .bat in this
REM  folder. Short version: cmd.exe miscounts its read position when a
REM  batch file holds multi-byte characters, which makes lines vanish
REM  and run as bogus commands.
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

uv sync --quiet
if errorlevel 1 (
    type "config\sync-failed.txt"
    echo.
    pause
    exit /b 1
)

uv run --quiet python -X utf8 -m wenhui.launcher setkey

echo.
pause
