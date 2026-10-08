@echo off
rem Strata Manager - the external local GUI (Manager / Chat / Monitor / About) for the official
rem Strata checkout.  Lives OUTSIDE the repo: it manages STRATA_ROOT (see manager.env) by reading
rem the official setup.py/configs and launching the official serve/server.py - never by patching
rem official files.  Future Strata updates cannot conflict with it.
rem
rem This launcher opens NO console window: it runs the Manager with pythonw.exe (Windows GUI mode)
rem and returns immediately, so the browser page is the only thing the user sees.  The Manager's
rem own output (or a startup error) goes to logs\manager.log instead of a terminal.
setlocal
cd /d "%~dp0"

rem ---- configuration: STRATA_ROOT / STRATA_API / MANAGER_PORT live in manager.env --------------
rem (the Manager reads that file itself; this launcher only needs STRATA_ROOT for the python it
rem prefers to run the Manager with)
set "STRATA_ROOT="
for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0manager.env") do if /i "%%A"=="STRATA_ROOT" set "STRATA_ROOT=%%~B"
if not defined STRATA_ROOT set "STRATA_ROOT=C:\AI\Runtime\Strata"

rem ---- prefer the Strata venv's pythonw.exe (console-less: no black window, ever) ---------------
set "PYW="
if exist "%STRATA_ROOT%\.venv\Scripts\pythonw.exe" set "PYW=%STRATA_ROOT%\.venv\Scripts\pythonw.exe"
if defined PYW goto run
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if defined PYW goto run

rem ---- console-less interpreters on PATH ---------------------------------------------------------
where pyw.exe >nul 2>nul
if not errorlevel 1 set "PYW=pyw -3"
if defined PYW goto run
where pythonw.exe >nul 2>nul
if not errorlevel 1 set "PYW=pythonw"
if defined PYW goto run

rem ---- fallback: a console python (the Manager still works, just with a window) ------------------
where py.exe >nul 2>nul
if not errorlevel 1 set "PYW=py -3"
if defined PYW goto run
set "PYW=python"
where python.exe >nul 2>nul
if not errorlevel 1 goto run
goto no_pythonw

:run
if not exist "%~dp0logs" mkdir "%~dp0logs"
rem pythonw: no console, ever.  start: the cmd window itself goes away immediately.
start "" %PYW% "%~dp0gui\manager.py" --root "%STRATA_ROOT%" %*
exit /b 0

:no_pythonw
echo  Python 3.10 or newer is not installed. Run START-HERE.bat in %STRATA_ROOT% once first.
pause
exit /b 1