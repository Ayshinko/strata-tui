@echo off
rem STRATA-TUI - a minimal terminal launcher for Strata (single-file / portable).
rem
rem Finds the Python to run the launcher with: the Strata venv's python when this
rem folder sits inside (or next to) a Strata checkout, else py -3, else python.
rem The launcher itself resolves the checkout: this folder, a strata-root.txt
rem next to it, or the STRATA_ROOT environment variable.
setlocal
cd /d "%~dp0"

rem ---- prefer the Strata venv's python if a Strata checkout is in reach ----
set "STRATA_PY="
if exist ".venv\Scripts\python.exe" set "STRATA_PY=.venv\Scripts\python.exe"
if defined STRATA_PY goto run

set "SR_ROOT="
if exist "strata-root.txt" set /p SR_ROOT=<"strata-root.txt"
if defined SR_ROOT if exist "%SR_ROOT%\.venv\Scripts\python.exe" set "STRATA_PY=%SR_ROOT%\.venv\Scripts\python.exe"
if defined STRATA_PY goto run

if defined STRATA_ROOT if exist "%STRATA_ROOT%\.venv\Scripts\python.exe" set "STRATA_PY=%STRATA_ROOT%\.venv\Scripts\python.exe"
if defined STRATA_PY goto run

:run
if defined STRATA_PY (
    "%STRATA_PY%" "%~dp0STRATA-TUI.py" %*
    exit /b %errorlevel%
)
where py.exe >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0STRATA-TUI.py" %*
    exit /b %errorlevel%
)
python "%~dp0STRATA-TUI.py" %*