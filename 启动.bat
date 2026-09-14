@echo off
REM ===================================================================
REM  Wenhui launcher.
REM
REM  !! KEEP THIS FILE PURE ASCII !!
REM  Do not add Chinese characters here. cmd.exe reads batch files by
REM  advancing a pointer by CHARACTER count while the file is UTF-8, so
REM  every multi-byte character shifts it out of step. The symptom is a
REM  whole line vanishing and the tail of the next line being run as a
REM  command, printing "is not recognized as an internal or external
REM  command". Yes, that includes box-drawing characters like the ones
REM  this file used to print -- those are multi-byte too.
REM
REM  All Chinese lives in src/wenhui/launcher.py (Python handles UTF-8
REM  correctly) and in config/*.txt (printed with `type`, which copies
REM  bytes straight to the console without the parser touching them).
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

uv run --quiet python -X utf8 -m wenhui.launcher start

echo.
pause
