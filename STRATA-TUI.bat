@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "STRATA-TUI.py"
    exit /b %errorlevel%
)
where py.exe >nul 2>nul
if not errorlevel 1 (
    py -3 "STRATA-TUI.py"
    exit /b %errorlevel%
)
python "STRATA-TUI.py"
