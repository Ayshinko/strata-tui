@echo off
rem STRATA-TUI - lightweight terminal controls for Strata. Does not modify Strata source.
setlocal
cd /d "%~dp0"

for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0manager.env") do (
  set "line=%%A"
  if not "!line:~0,1!"=="#" set "%%A=%%B"
)
if not defined STRATA_ROOT set "STRATA_ROOT=C:\AI\Runtime\Strata"

rem a console python is required here (the TUI owns the terminal) - prefer the Strata venv
set "PY="
if exist "%STRATA_ROOT%\.venv\Scripts\python.exe" set "PY=%STRATA_ROOT%\.venv\Scripts\python.exe"
if defined PY goto run
where py.exe >nul 2>nul
if not errorlevel 1 set "PY=py -3" & goto run
where python.exe >nul 2>nul
if not errorlevel 1 set "PY=python" & goto run
echo  Python 3.10 or newer is not installed. Run START-HERE.bat in %STRATA_ROOT% once first.
pause
exit /b 1

:run
%PY% "%~dp0STRATA-TUI.py" %*
exit /b %errorlevel%